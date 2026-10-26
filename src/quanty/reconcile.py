"""A live session's check on start: what its strategies hold for real, rebuilt from the run's fills
and settlements, against what each venue says the account holds.

A fill quanty never saw (an order that filled while no session ran, a manual trade, another program
on the account) leaves the two apart. Where the venue holds less than quanty thinks, or the other
side, quanty's position can't be trusted: a strategy would size, stop or exit on contracts that
aren't there. Those strategies are halted (no new buys; exits still allowed) until someone checks the
venue and resumes them. Where the venue holds more, in a market this run has traded for real, it's
reported and nothing stops: the account may have other traders. Markets that settled while no
session ran don't count; they settle in quanty when the venue next reports the result.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from quanty.brokers.live import LiveBroker
from quanty.core.engine import Engine
from quanty.db.models import FillRow
from quanty.venues.orders import OrderAPI, VenueError, native_id

EPS = 1e-6


@dataclass(frozen=True)
class Finding:
    market_id: str
    ours: float  # net YES contracts this run's strategies hold for real (− = NO)
    venue: float  # what the venue says (NaN: couldn't read it)
    instances: tuple[str, ...]  # the strategies holding it
    halts: bool  # quanty's position can't be trusted: its strategies stop buying

    def text(self) -> str:
        if math.isnan(self.venue):
            return (
                f"{self.market_id}: holds {_contracts(self.ours)}, but the venue's positions couldn't be read"
            )
        held = f"the venue holds {_contracts(self.venue)}, quanty's strategies {_contracts(self.ours)}"
        if self.halts:
            return f"{self.market_id}: {held}; check the venue, then resume {', '.join(self.instances)}"
        return f"{self.market_id}: {held} (another program on the account, a manual trade, or a missed fill)"


def _contracts(net: float) -> str:
    return "nothing" if abs(net) < EPS else f"{abs(net):g} {'YES' if net > 0 else 'NO'}"


def compare(
    ours: Mapping[str, Mapping[str, float]], venue: Mapping[str, float], settled: set[str], traded: set[str]
) -> list[Finding]:
    """ours: market -> instance -> net held; venue: market -> net; settled: markets that settled;
    traded: markets this run has traded for real (where more than ours on the venue is worth a word)."""
    out = []
    for mid, by_inst in sorted(ours.items()):
        mine = sum(by_inst.values())
        if abs(mine) < EPS or mid in settled:
            continue
        theirs = venue.get(mid, 0.0)
        same_side = theirs * mine > 0
        if same_side and abs(theirs) >= abs(mine) - EPS:
            if abs(theirs) > abs(mine) + EPS:
                out.append(Finding(mid, mine, theirs, tuple(sorted(by_inst)), halts=False))
            continue
        out.append(Finding(mid, mine, theirs, tuple(sorted(by_inst)), halts=True))
    for mid in sorted(traded - set(ours) - settled):
        if abs(venue.get(mid, 0.0)) > EPS:
            out.append(Finding(mid, 0.0, venue[mid], (), halts=False))
    return out


async def check(
    engine: Engine, apis: Mapping[str, OrderAPI], client: httpx.AsyncClient, traded: set[str]
) -> list[Finding]:
    """Read each venue's positions where this run holds or traded something for real, and compare."""
    ours = engine.live_holdings()
    findings: list[Finding] = []
    for venue in sorted({m.split(":", 1)[0] for m in set(ours) | traded}):
        api = apis.get(venue)
        if api is None:
            continue
        mine = {m: v for m, v in ours.items() if m.startswith(f"{venue}:")}
        try:
            h = await api.holdings(client, {native_id(m) for m in mine})
        except (httpx.HTTPError, VenueError, RuntimeError, ValueError, NotImplementedError):
            findings += [  # unverified: treated as not there
                Finding(m, sum(v.values()), math.nan, tuple(sorted(v)), halts=True)
                for m, v in sorted(mine.items())
                if abs(sum(v.values())) > EPS
            ]
            continue
        net = {f"{venue}:{k}": q for k, q in h.net.items()}
        settled = {f"{venue}:{k}" for k in h.settled}
        findings += compare(mine, net, settled, {m for m in traded if m.startswith(f"{venue}:")})
    return findings


def traded_for_real(engine: Engine) -> set[str]:
    """Markets this run has had real fills in."""
    rows = engine.ledger.execute(select(FillRow.market_id).where(FillRow.kind == "live").distinct())
    return set(rows.scalars())


async def on_start(engine: Engine) -> list[Finding]:
    """The check for a live session about to start: halts what can't be trusted, logs every finding."""
    live = next((b for b in engine.brokers.values() if isinstance(b, LiveBroker)), None)
    if live is None:
        return []
    traded = traded_for_real(engine)
    if not traded and not engine.live_holdings():
        return []  # never traded for real: nothing to compare
    async with httpx.AsyncClient(timeout=10, transport=live.transport) as client:
        findings = await check(engine, live.apis, client, traded)
    for f in findings:
        engine.ledger.log(engine.now, "reconcile", "positions", {"detail": f.text()},
                          level="error" if f.halts else "warning")  # fmt: skip
        for iid in f.instances if f.halts else ():
            if iid not in engine.halted:
                engine.halt(iid, f"positions differ from the venue's: {f.text()}")
    halted = sorted({i for f in findings if f.halts for i in f.instances})
    engine.ledger.log(
        engine.now, "reconcile", "positions checked", {"differences": len(findings), "halted": halted}
    )
    return findings
