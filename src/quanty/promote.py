"""`quanty promote <instance> --to sim|shadow|live`: move one instance onto another of the experiment's
brokers by editing its file (comments and layout kept), so paper becomes shadow becomes live, or back.

It only edits the file. Nothing trades until `quanty paper` or `quanty live --arm` runs it; a
running session keeps its brokers until restarted. Paper and live sessions of an experiment record
into one run, so the instance keeps its history; each order records whether it was real money.

Going live keeps a paper twin (`<id>_paper`, `twin_of = "<id>"`): the same strategy, params and
limits on the simulated broker it came from, in the same session, so the dashboard's Compare puts
live fills beside simulated ones on the same markets at the same moments. Stepping back removes it.
Dashboard edits to the instance's params are written into the file first, so both start equal.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from quanty import experiment

KINDS = ("sim", "shadow", "live")


@dataclass
class Promotion:
    instance: str
    from_broker: str
    from_kind: str
    to_broker: str
    to_kind: str
    capped: bool = False  # set the live broker's caps
    carried: dict[str, Any] = field(default_factory=dict)  # dashboard edits written into the file
    twin_added: str | None = None
    twin_removed: str | None = None

    def lines(self) -> list[str]:
        was, now = f"{self.from_broker} ({self.from_kind})", f"{self.to_broker} ({self.to_kind})"
        out = [f"{self.instance}: broker {was} → {now}"]
        if self.capped:
            out.append(f"set the caps of [brokers.{self.to_broker}]")
        if self.carried:
            out.append(
                "carried dashboard edits into the file: "
                + ", ".join(f"{k} = {v}" for k, v in self.carried.items())
            )
        if self.twin_added:
            out.append(
                f"added its paper twin {self.twin_added} on {self.from_broker}, for the dashboard's Compare"
            )
        if self.twin_removed:
            out.append(f"removed its paper twin {self.twin_removed} (its history stays in the run)")
        return out


def promote(
    path: Path,
    instance: str,
    to: str,
    *,
    broker: str | None = None,
    max_qty: float | None = None,
    max_notional: float | None = None,
    overrides: dict[str, Any] | None = None,
    twin: bool = True,
) -> Promotion:
    """Edit the experiment at `path` so `instance` runs on a `to` broker: `broker` names one of the
    experiment's, else the built-in one of that kind. A live broker without caps needs max_qty and
    max_notional (written into its table). `overrides` (the instance's dashboard edits) are written
    into its params. twin: going live from paper adds a paper twin; going back removes it."""
    if to not in KINDS:
        raise ValueError(f"--to must be one of {', '.join(KINDS)}")
    exp = experiment.load(path)  # a valid file to start from
    spec = next((i for i in exp.instances if i.id == instance), None)
    if spec is None:
        raise ValueError(
            f"{path.name} has no instance {instance!r} (has: {', '.join(i.id for i in exp.instances)})"
        )
    from_name = exp.broker_of(spec)
    from_kind = exp.brokers[from_name].kind
    target = broker or to
    if target not in exp.brokers:
        raise ValueError(f"{path.name} has no broker {target!r} (has: {', '.join(exp.brokers)})")
    if exp.brokers[target].kind != to:
        raise ValueError(f"broker {target!r} is {exp.brokers[target].kind}, not {to}")
    if target == from_name:
        raise ValueError(f"{instance} already runs on {from_name} ({from_kind})")

    doc = tomlkit.parse(path.read_text())
    result = Promotion(instance, from_name, from_kind, target, to)
    cfg = exp.brokers[target]
    if isinstance(cfg, experiment.LiveBrokerConfig) and not cfg.capped:
        if max_qty is None or max_notional is None:
            raise ValueError(
                f"[brokers.{target}] has no caps: give --max-qty and --max-notional "
                "(the largest single order)"
            )
        if "brokers" not in doc:
            doc["brokers"] = tomlkit.table(is_super_table=True)
        if target not in doc["brokers"]:
            new = tomlkit.table()
            new.add(tomlkit.nl())
            doc["brokers"][target] = new
        doc["brokers"][target]["max_qty"] = max_qty
        doc["brokers"][target]["max_notional"] = max_notional
        result.capped = True
    entries = doc.get("instances", [])
    entry = next(e for e in entries if e.get("id") == instance)
    entry["broker"] = target
    if overrides:
        if "params" not in entry:
            entry["params"] = tomlkit.table()
        for k, v in overrides.items():
            entry["params"][k] = v
        result.carried = dict(overrides)
    twins = [i for i, e in enumerate(entries) if e.get("twin_of") == instance]
    if to == "live" and from_kind != "live" and twin and not twins:
        name = _free_id(f"{instance}_paper", exp)
        copy = tomlkit.table()
        copy["id"] = name
        for k in ("strategy", "series"):
            if k in entry:
                copy[k] = entry[k]
        copy["broker"] = from_name
        copy["twin_of"] = instance
        for k in ("params", "limits"):
            if k in entry:
                copy[k] = tomlkit.parse(tomlkit.dumps({k: entry[k]}))[k]  # a copy, comments and all
        entry.add(tomlkit.nl())  # a blank line between the entries, as people write them
        entries.insert(next(i for i, e in enumerate(entries) if e.get("id") == instance) + 1, copy)
        result.twin_added = name
    elif to != "live" and twins:
        result.twin_removed = str(entries[twins[0]].get("id"))
        for i in reversed(twins):
            del entries[i]
    text = tomlkit.dumps(doc)
    try:
        experiment.parse(tomllib.loads(text), source=path.name)
    except ValueError as exc:
        raise ValueError(f"not changed: {path.name} wouldn't load afterwards ({exc})") from None
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return result


def _free_id(name: str, exp: experiment.Experiment) -> str:
    ids = {i.id for i in exp.instances}
    n, i = name, 2
    while n in ids:
        n, i = f"{name}{i}", i + 1
    return n


def run_name(path: Path) -> str:
    """The dashboard run this experiment's sessions write to, paper or live: session/<name>."""
    return f"session/{path.stem}"


def needs_caps(path: Path, to: str, broker: str | None) -> bool:
    """Whether promoting to `to` needs caps first: its live broker has none yet."""
    if to != "live":
        return False
    cfg = experiment.load(path).brokers.get(broker or to)
    return isinstance(cfg, experiment.LiveBrokerConfig) and not cfg.capped
