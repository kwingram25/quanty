"""`quanty promote <instance> --to sim|shadow|live`: move one instance onto another kind of broker by
editing its experiment file (comments and layout kept), so paper becomes shadow becomes live, or back.

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
DEFAULT_NAMES = {"sim": "sim", "shadow": "shadow", "live": "live"}


@dataclass
class Promotion:
    instance: str
    from_broker: str
    from_kind: str
    to_broker: str
    to_kind: str
    added_broker: bool = False
    carried: dict[str, Any] = field(default_factory=dict)  # dashboard edits written into the file
    pinned: list[str] = field(default_factory=list)  # other instances given their broker explicitly
    twin_added: str | None = None
    twin_removed: str | None = None

    def lines(self) -> list[str]:
        was, now = f"{self.from_broker} ({self.from_kind})", f"{self.to_broker} ({self.to_kind})"
        out = [f"{self.instance}: broker {was} → {now}"]
        if self.added_broker:
            out.append(f"added [brokers.{self.to_broker}] (kind = {self.to_kind!r})")
        if self.pinned:
            out.append(
                f"set broker = {self.from_broker!r} on {', '.join(self.pinned)} (now that there are two)"
            )
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
    """Edit the experiment at `path` so `instance` runs on a `to` broker: `broker` names one (made if
    missing), else the first broker of that kind, else a new one. A new live broker needs max_qty and
    max_notional. `overrides` (the instance's dashboard edits) are written into its params. twin:
    going live from paper adds a paper twin; going back removes it."""
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
    if broker is not None and broker in exp.brokers and exp.brokers[broker].kind != to:
        raise ValueError(f"broker {broker!r} is {exp.brokers[broker].kind}, not {to}")
    target = (
        broker
        or next((n for n, b in exp.brokers.items() if b.kind == to), None)
        or _free(DEFAULT_NAMES[to], exp)
    )
    if target == from_name:
        raise ValueError(f"{instance} already runs on {from_name} ({from_kind})")

    doc = tomlkit.parse(path.read_text())
    result = Promotion(instance, from_name, from_kind, target, to)
    if "brokers" not in doc:  # the implicit default ([brokers.sim]) becomes explicit
        brokers = tomlkit.table(is_super_table=True)
        brokers[from_name] = {"kind": from_kind}
        doc["brokers"] = brokers
    if target not in exp.brokers:
        if to == "live" and (max_qty is None or max_notional is None):
            raise ValueError(
                "a new live broker needs --max-qty and --max-notional (the largest single order)"
            )
        new = tomlkit.table()
        new["kind"] = to
        if to == "live":
            new["max_qty"] = max_qty
            new["max_notional"] = max_notional
        else:
            src = exp.brokers[from_name]
            if src.kind != "live":  # keep the simulated latency it had
                new["latency_ms"] = src.model_dump()["latency_ms"]
        new.add(tomlkit.nl())
        doc["brokers"][target] = new
        result.added_broker = True
    entries = doc.get("instances", [])
    for e in entries:  # others that relied on "the one broker" keep it explicitly
        if "broker" not in e and e.get("id") != instance:
            e["broker"] = from_name
            result.pinned.append(str(e.get("id")))
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


def _free(name: str, exp: experiment.Experiment) -> str:
    n, i = name, 2
    while n in exp.brokers:
        n, i = f"{name}{i}", i + 1
    return n


def run_name(path: Path) -> str:
    """The dashboard run this experiment's sessions write to, paper or live: session/<name>."""
    return f"session/{path.stem}"


def needs_caps(path: Path, to: str, broker: str | None) -> bool:
    """Whether promoting to `to` would add a live broker, which needs max_qty and max_notional."""
    if to != "live":
        return False
    exp = experiment.load(path)
    if broker is not None:
        return broker not in exp.brokers
    return not any(b.kind == "live" for b in exp.brokers.values())
