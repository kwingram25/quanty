"""`quanty doctor`: check a project before trading, and say how to fix what's wrong.

Configuration (experiments, strategies, feeds), keys (present, valid, private), the clock against
each venue's, each venue (reachable, keys accepted, rate-limit headroom), orders a previous live run
left unresolved, and disk space. Network checks only read: a balance and, on Kalshi, the account's
limits. Nothing is placed or changed.
"""

import asyncio
import json
import shutil
import stat
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal

import httpx

from quanty.venues.orders import OrderAPI, VenueError, default_apis

Status = Literal["ok", "warn", "fail", "skip"]
DRIFT_WARN_S, DRIFT_FAIL_S = 2.0, 5.0  # signed requests carry a timestamp the venue checks


@dataclass(frozen=True)
class Check:
    section: str
    name: str
    status: Status
    detail: str
    fix: str = ""


def run(project: Path, experiments: list[Path], feeds: Path | None, data: Path, offline: bool) -> list[Check]:
    out: list[Check] = []
    venues = _config(out, project, experiments, feeds)
    _keys(out, project, venues)
    if offline:
        out.append(Check("Venues", "network", "skip", "skipped (--offline)"))
    else:
        out += asyncio.run(_venues(venues))
    _live(out, data)
    _disk(out, data)
    return out


# --- configuration ---


def _config(out: list[Check], project: Path, experiments: list[Path], feeds: Path | None) -> set[str]:
    from quanty import config, experiment, plugins
    from quanty.feeds.cli import load_feeds

    sec = "Configuration"
    cfg = config.load()
    try:
        found = plugins.discover(cfg.strategy_paths, python_paths=cfg.python_paths)
    except Exception as exc:  # a strategy that doesn't import
        out.append(
            Check(sec, "strategies", "fail", f"a strategy failed to load: {exc}", "fix the file it names")
        )
        found = {}
    else:
        mine = sorted(n for n, m in found.items() if Path(m.__file__ or "").parent != plugins.EXAMPLES)
        out.append(
            Check(
                sec,
                "strategies",
                "ok" if mine else "warn",
                f"{len(mine)} of your own: {', '.join(mine)}" if mine else "only the bundled examples",
                "" if mine else "add one in strategies/ (quanty init writes a skeleton)",
            )
        )
    venues: set[str] = set()
    if not experiments:
        out.append(
            Check(
                sec, "experiments", "warn", "none found", "add experiments/main.toml (quanty init writes one)"
            )
        )
    for path in experiments:
        name = str(path.relative_to(project)) if path.is_relative_to(project) else str(path)
        try:
            exp = experiment.load(path)
            instances = experiment.build(exp, found)
        except (ValueError, OSError) as exc:
            out.append(Check(sec, name, "fail", str(exc).splitlines()[0], "fix the field it names"))
            continue
        live = experiment.live_brokers(exp)
        detail = f"{len(instances)} instance(s): {', '.join(i.id for i in instances)}"
        out.append(Check(sec, name, "ok", detail + (f"; on live brokers: {', '.join(live)}" if live else "")))
        live_cfg = exp.brokers.get("live")
        if isinstance(live_cfg, experiment.LiveBrokerConfig):
            if live_cfg.capped:
                caps = f"up to {live_cfg.max_qty:g} contracts and ${live_cfg.max_notional:g} per order"
                out.append(Check(sec, f"{name} live caps", "ok", caps))
            else:
                detail = "the live broker has no caps, so nothing can go live"
                fix = "set max_qty and max_notional under [brokers.live]"
                out.append(Check(sec, f"{name} live caps", "skip", detail, fix))
        for inst in instances:
            venues |= {s.split(":", 1)[0] for s in inst.strategy.series if ":" in s}
    if feeds is None or not feeds.exists():
        out.append(
            Check(sec, "feeds", "warn", "no feeds.toml: paper and live need one", "quanty init writes one")
        )
    else:
        try:
            loaded = load_feeds(feeds, None)
            kinds = {f.config.kind for f in loaded.values()}
        except (ValueError, OSError) as exc:
            out.append(Check(sec, "feeds.toml", "fail", str(exc).splitlines()[0], "fix the field it names"))
        else:
            out.append(Check(sec, "feeds.toml", "ok", ", ".join(sorted(kinds))))
            venues |= kinds
            indexes = {i for f in loaded.values() for i in getattr(f.config, "index_ids", ())}
            if "polymarket_us" in kinds and "BRTI" not in indexes:
                detail = "Polymarket US BTC markets settle on cfbenchmarks:BRTI; no feed here streams it"
                fix = 'add [feeds.kalshi] with index_ids = ["BRTI"] (needs Kalshi keys)'
                out.append(Check(sec, "index data", "warn", detail, fix))
    try:
        known = set(default_apis())
    except (ImportError, ValueError) as exc:
        out.append(Check(sec, "extensions", "fail", str(exc).splitlines()[0], "fix the extension it names"))
        return set()
    return venues & known


# --- keys ---


def _keys(out: list[Check], project: Path, venues: set[str]) -> None:
    from quanty.venues.auth import keys

    sec = "Keys"
    keys.cache_clear()  # read this project's .env, not one read earlier
    env = project / ".env"
    if env.exists():
        mode = stat.S_IMODE(env.stat().st_mode)
        private = mode & 0o077 == 0
        out.append(
            Check(
                sec,
                ".env",
                "ok" if private else "warn",
                "readable only by you" if private else f"readable by others (mode {mode:o})",
                "" if private else "chmod 600 .env",
            )
        )
    apis = default_apis() if venues else {}  # fresh: their signers read the keys just cleared
    for venue in sorted(venues):
        api = apis[venue]
        label = api.label or venue
        try:
            api.signer  # noqa: B018 - reads and checks the keys
        except RuntimeError as exc:
            out.append(Check(sec, label, "fail", str(exc), "add the key to .env"))
        except OSError as exc:
            detail = f"can't read the key file: {exc.strerror or exc}"
            fix = (
                f"put the key itself in .env as {api.key_env}"
                if api.key_env
                else "put the key itself in .env"
            )
            out.append(Check(sec, label, "fail", detail, fix))
        except (ValueError, TypeError) as exc:
            out.append(Check(sec, label, "fail", f"key isn't valid: {exc}", "re-copy it from the venue"))
        else:
            warning = api.key_warning()
            if warning is not None:
                out.append(Check(sec, label, "warn", *warning))
            else:
                out.append(Check(sec, label, "ok", "keys present and well-formed"))
    pw = keys().quanty_ui_password
    if pw is None:
        out.append(Check(sec, "dashboard login", "ok", "off (local use; see docs/deploying.md to expose it)"))
    elif pw.get_secret_value().strip().startswith("scrypt$"):
        out.append(Check(sec, "dashboard login", "ok", "on"))
    else:
        out.append(
            Check(
                sec,
                "dashboard login",
                "fail",
                "QUANTY_UI_PASSWORD isn't a hash",
                "quanty password --write",
            )
        )


# --- venues ---


async def _venues(
    venues: set[str],
    apis: dict[str, OrderAPI] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Check]:
    if not venues:
        return [Check("Venues", "venues", "skip", "no venue in the experiments or feeds.toml")]
    apis = apis or default_apis()
    async with httpx.AsyncClient(timeout=10, transport=transport) as client:
        results = await asyncio.gather(*(_probe(client, v, apis[v]) for v in sorted(venues)))
    return [c for checks in results for c in checks]


async def _probe(client: httpx.AsyncClient, venue: str, api: OrderAPI) -> list[Check]:
    sec, label = "Venues", api.label or venue
    out: list[Check] = []
    t0 = time.perf_counter()
    try:
        bal = await api.balance(client)
    except RuntimeError:
        return [Check(sec, label, "skip", "no keys: connection not checked")]
    except VenueError as exc:
        rtt = (time.perf_counter() - t0) * 1000
        if exc.status == 429:
            out.append(
                Check(
                    sec,
                    label,
                    "warn",
                    f"reachable ({rtt:.0f} ms) but rate limited right now",
                    "something else is using this account's request budget (a running trader?)",
                )
            )
        elif exc.status in (401, 403):
            out.append(
                Check(sec, label, "fail", f"keys rejected (HTTP {exc.status})", "check the key id and key")
            )
        else:
            out.append(Check(sec, label, "fail", f"HTTP {exc.status}: {exc.detail[:120]}"))
    except httpx.HTTPError as exc:
        return [
            Check(sec, label, "fail", f"unreachable: {type(exc).__name__}", "check the network / firewall")
        ]
    else:
        rtt = (time.perf_counter() - t0) * 1000
        out.append(Check(sec, label, "ok", f"keys accepted, {rtt:.0f} ms; balance ${bal.cash:,.2f}"))
    out.append(await _clock(client, api.base_url, label))
    try:
        out.append(Check(sec, f"{label} rate limit", "ok", await api.limits(client)))
    except (VenueError, httpx.HTTPError) as exc:
        out.append(Check(sec, f"{label} rate limit", "skip", f"couldn't read the account's limits ({exc})"))
    return out


async def _clock(client: httpx.AsyncClient, base_url: str, label: str) -> Check:
    try:
        t0 = time.time()
        resp = await client.head(base_url)
        mid = (t0 + time.time()) / 2
        stamp = resp.headers.get("date")
        if not stamp:
            return Check("Venues", f"{label} clock", "skip", "the venue sent no time to compare with")
        # Date drops the fraction of a second, so on average it reads 0.5 s behind the venue
        drift = mid - parsedate_to_datetime(stamp).timestamp() - 0.5
    except (httpx.HTTPError, ValueError, TypeError):
        return Check("Venues", f"{label} clock", "skip", "couldn't read the venue's time")
    status: Status = "fail" if abs(drift) > DRIFT_FAIL_S else "warn" if abs(drift) > DRIFT_WARN_S else "ok"
    word = "ahead of" if drift > 0 else "behind"
    return Check(
        "Venues",
        f"{label} clock",
        status,
        f"{abs(drift):.1f} s {word} the venue",
        "" if status == "ok" else "turn on network time sync (signed requests carry a timestamp)",
    )


# --- live safety and disk ---


def _live(out: list[Check], data: Path) -> None:
    unresolved = 0
    journals = [*(data / "session").glob("*.orders.jsonl"), *(data / "live").glob("*.orders.jsonl")]
    for journal in sorted(journals):
        state: dict[str, dict[str, object]] = {}
        for line in journal.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            state.setdefault(rec.get("order", ""), {}).update(rec)
        unresolved += sum(1 for r in state.values() if not r.get("done") and not r.get("settled"))
    if unresolved:
        out.append(
            Check(
                "Live",
                "previous run",
                "warn",
                f"{unresolved} order(s) a live run left unresolved",
                "check them on the venue; `quanty live --arm` cancels any still resting on start",
            )
        )
    else:
        out.append(Check("Live", "previous run", "ok", "no unresolved orders"))


def _disk(out: list[Check], data: Path) -> None:
    where = data if data.exists() else data.parent if data.parent.exists() else Path(".")
    free_gb = shutil.disk_usage(where).free / 1e9
    status: Status = "fail" if free_gb < 0.5 else "warn" if free_gb < 2 else "ok"
    out.append(
        Check(
            "Disk",
            "free space",
            status,
            f"{free_gb:.1f} GB free where data/ lives",
            "" if status == "ok" else "recordings grow by GBs a day: free some space",
        )
    )
