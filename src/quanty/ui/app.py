"""The web UI: a read-only dashboard over ledgers (data/{live,paper,backtest}/*.sqlite) and feed
metrics (data/quanty.sqlite). Run it with `quanty ui`."""

import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from quanty.ui import charts, queries

HERE = Path(__file__).parent


def _money(v: float | None) -> str:
    v = v or 0.0
    return f"{'−' if v < -0.005 else ''}${abs(v):,.2f}"


def _sign(v: float | None) -> str:
    return "pos" if (v or 0) > 0.005 else "neg" if (v or 0) < -0.005 else ""


def _ago(ms: int | None) -> str:
    if not ms:
        return "never"
    s = max(0, time.time() - ms / 1000)
    if s < 90:
        return f"{s:.0f} s ago"
    if s < 5400:
        return f"{s / 60:.0f} min ago"
    if s < 172_800:
        return f"{s / 3600:.0f} h ago"
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def _clock(ms: int) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.gmtime(ms / 1000))


def _ms(v: float | None) -> str:
    return "–" if v is None else f"{v:.0f} ms"


def _brief(fields: dict[str, Any] | None) -> str:
    if not fields:
        return ""
    keep = {k: v for k, v in fields.items() if k not in ("request",)}
    return json.dumps(keep, default=str)[:300]


def create_app(data: Path, refresh_s: int = 5) -> FastAPI:
    app = FastAPI(title="quanty", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(money=_money, sign=_sign, ago=_ago, clock=_clock, ms=_ms, brief=_brief)
    env.globals.update(spark=charts.sparkline, refresh_s=refresh_s, data=data, hours=2)

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        runs = [queries.view(r) for r in queries.find_runs(data)]
        return page(request, "overview.html", page="overview", runs=runs, feeds=queries.feed_states(data))

    @app.get("/feeds", response_class=HTMLResponse)
    def feeds(request: Request) -> HTMLResponse:
        return page(request, "feeds.html", page="feeds", feeds=queries.feed_states(data))

    @app.get("/runs/{mode}/{name}", response_class=HTMLResponse)
    def run(request: Request, mode: str, name: str) -> HTMLResponse:
        r = queries.run_by_name(data, f"{mode}/{name}")
        if r is None:
            raise HTTPException(404, f"no run {mode}/{name}")
        v = queries.view(r)
        curves = queries.pnl_curves(r)
        slots = charts.color_slots([s.instance for s in v.summaries])
        chart = charts.step_chart(curves, slots, label=f"Cumulative realized P&L, {r.name}")
        return page(
            request,
            "run.html",
            page="run",
            v=v,
            curves=curves,
            slots=slots,
            chart=chart,
            positions=queries.positions(r),
            recent=queries.recent(r),
        )

    return app
