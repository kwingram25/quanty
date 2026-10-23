"""The web UI: a read-only dashboard over ledgers (data/{live,paper,backtest}/*.sqlite) and feed
metrics (data/quanty.sqlite). Run it with `quanty ui`."""

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from quanty.control import ControlStore
from quanty.core.ledger import Summary
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

    token = secrets.token_urlsafe(24)  # in every form; a page on another site can't read it
    control_path = data / "control.sqlite"

    def control_view(run: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if not control_path.exists():
            return None, []
        store = ControlStore(control_path, create=False)
        try:
            return store.session(run), store.history(run)
        finally:
            store.close()

    @app.post("/runs/{mode}/{name}/control")
    async def control(request: Request, mode: str, name: str) -> RedirectResponse:
        run_name = f"{mode}/{name}"
        back = f"/runs/{run_name}"
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "cross-site request refused")
        form = await request.form()
        if form.get("token") != token:
            raise HTTPException(403, "missing or stale form token: reload the page")
        if queries.run_by_name(data, run_name) is None:
            raise HTTPException(404, f"no run {run_name}")
        action, _, target = str(form.get("cmd", "")).partition(":")

        def done(msg: str, bad: bool = False) -> RedirectResponse:
            return RedirectResponse(f"{back}?{urlencode({'msg': msg, 'bad': int(bad)})}", status_code=303)

        if mode == "live" and form.get("confirm") != name:
            return done(f"not sent: type {name} to confirm actions on a live run", bad=True)
        store = ControlStore(control_path)
        try:
            if store.session(run_name) is None:
                return done("not sent: this run is not running", bad=True)
            try:
                cid = store.submit(run_name, action, target)
            except ValueError as exc:
                return done(f"not sent: {exc}", bad=True)
            for _ in range(30):  # sessions poll every second; wait briefly for the outcome
                await asyncio.sleep(0.1)
                row = next((c for c in store.history(run_name) if c["id"] == cid), None)
                if row is not None and row["status"] != "pending":
                    return done(row["result"], bad=row["status"] == "failed")
            return done(f"sent {action} {target or 'all'}; waiting for the session")
        finally:
            store.close()

    @app.get("/runs/{mode}/{name}", response_class=HTMLResponse)
    def run(request: Request, mode: str, name: str) -> HTMLResponse:
        r = queries.run_by_name(data, f"{mode}/{name}")
        if r is None:
            raise HTTPException(404, f"no run {mode}/{name}")
        v = queries.view(r)
        curves = queries.pnl_curves(r)
        slots = charts.color_slots([s.instance for s in v.summaries])
        chart = charts.step_chart(curves, slots, label=f"Cumulative realized P&L, {r.name}")
        session, history = control_view(r.name)
        # every strategy: those with activity, plus a running session's that haven't traded yet
        summaries = {x.instance: x for x in v.summaries}
        for iid in (session or {}).get("state", {}).get("instances", {}):
            summaries.setdefault(iid, Summary(iid, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0))
        strategies = [summaries[k] for k in sorted(summaries)]
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
            activity=queries.activity(r),
            strategies=strategies,
            session=session,
            history=history,
            token=token,
            msg=request.query_params.get("msg", ""),
            msg_bad=request.query_params.get("bad") == "1",
        )

    return app
