"""`quanty mcp`: the dashboard as tools for an AI agent (Claude Code, or any MCP client), so a
conversation can summarize strategies and change them.

It runs on your machine and talks to a dashboard: a local one, or a server's through an SSH tunnel
it opens itself (`--ssh you@server`). It can read and do exactly what the dashboard can, through
the same API and checks (the page token, the run's name for anything on a run with real money, caps
that can't be raised). It can't arm a broker or raise a cap, and nothing extra runs on the server.
Dashboard changes on a server are kept with the run; `quanty pull` brings them into your files.

A dashboard with a login reads its password from QUANTY_DASHBOARD_PASSWORD in the environment
(the password itself, never in a project file).
"""

import logging
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
CONFIRM = (
    "On a run with real money the dashboard asks for the run's name to confirm: pass `confirm` = the "
    "part after the slash (e.g. main for session/main), and only after the user has agreed to this "
    "exact change."
)

INSTRUCTIONS = """quanty trades prediction markets; this server is its dashboard. A run is one experiment's
record (session/<name> for paper and live sessions, backtest/<name>); each strategy in it runs on a
broker of kind sim (paper), shadow (orders checked by the venue, not placed) or live (real money).
Read before changing: list_runs, then run_summary. Changes apply to the running session at once and
are kept with the run (on a server, until `quanty pull` writes them into the project's files)."""


class Dashboard:
    """An HTTP client for a quanty dashboard: its JSON API, its login and its page token. What the
    dashboard refuses is a ToolError: the agent reads why."""

    def __init__(self, url: str, password: str | None = None, http: httpx.Client | None = None):
        self.url, self.password = url.rstrip("/"), password
        self.http = http or httpx.Client(base_url=self.url, timeout=20)
        self._token: str | None = None

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        r = self.http.request(method, path, **kw)
        if r.status_code == 401 or (
            r.status_code in (302, 303) and "/login" in r.headers.get("location", "")
        ):
            self._login()
            r = self.http.request(method, path, **kw)
        return r

    def _login(self) -> None:
        if not self.password:
            raise ToolError("the dashboard has a login: set QUANTY_DASHBOARD_PASSWORD to its password")
        r = self.http.post("/login", data={"password": self.password, "next": "/"})
        if r.status_code >= 400 or "Wrong password" in r.text or "quanty_session" not in self.http.cookies:
            raise ToolError("the dashboard refused the password in QUANTY_DASHBOARD_PASSWORD")

    def get(self, path: str, **params: Any) -> Any:
        r = self._send("GET", f"/api{path}", params={k: v for k, v in params.items() if v not in (None, "")})
        if r.status_code >= 400:
            raise ToolError(_detail(r))
        return r.json()

    def post(self, path: str, body: Any = None) -> Any:
        """A write, with the page's token."""
        for _ in range(2):  # a restarted dashboard has a new token: fetch it and try again
            if self._token is None:
                page = self._send("GET", "/").text
                self._token = page.split('name="quanty-token" content="')[1].split('"')[0]
            r = self._send("POST", f"/api{path}", json=body, headers={"x-quanty-token": self._token})
            if r.status_code == 403 and "token" in r.text:
                self._token = None
                continue
            break
        if r.status_code >= 400:
            raise ToolError(_detail(r))
        return r.json()

    def command(self, run: str, action: str, target: str = "", value: Any = None, confirm: str = "") -> str:
        body = {"action": action, "target": target, "value": value, "confirm": confirm}
        out = self.post(f"/runs/{run}/control", body)
        if not out.get("ok"):
            raise ToolError(str(out.get("result")))
        return str(out.get("result"))


def _detail(r: httpx.Response) -> str:
    try:
        return f"{r.status_code}: {r.json().get('detail')}"
    except ValueError:
        return f"{r.status_code}: {r.text[:200]}"


def _when(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds") if ms else None


def server(dash: Dashboard) -> MCPServer:
    mcp = MCPServer("quanty", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ)
    def list_runs() -> list[dict[str, Any]]:
        """Every run on the dashboard: whether it's running, paper or live, its P&L (real money and
        simulated apart where it has both), how many strategies, and when it last did anything."""
        return [
            {
                "run": r["name"],
                "label": r.get("label"),
                "running": r.get("running"),
                "pnl": round(r.get("pnl") or 0.0, 2),
                **({"real_pnl": r["real_pnl"], "sim_pnl": r.get("sim_pnl")} if "real_pnl" in r else {}),
                "fills": r.get("fills"),
                "strategies": r.get("strategies"),
                "errors": r.get("errors"),
                "last_activity": _when(r.get("last_ts")),
            }
            for r in dash.get("/runs")
        ]

    @mcp.tool(annotations=READ)
    def run_summary(run: str) -> dict[str, Any]:
        """One run's strategies: what each is, its params (and which were changed on the dashboard),
        its broker and kind (sim, shadow or live), whether it's halted, P&L, fills, wins and losses,
        stops, and the code it ran; plus the session's brokers when it's running. run: e.g. session/main."""
        snap = dash.get(f"/runs/{run}")
        session = snap.get("session") or {}
        stats = snap.get("order_stats") or {}
        strategies = []
        for s in snap.get("strategies", []):
            iid, info = s["instance"], s.get("info") or {}
            live = (session.get("instances") or {}).get(iid, {})
            strategies.append(
                {
                    "id": iid,
                    "strategy": info.get("strategy"),
                    "description": info.get("description"),
                    "kind": (snap.get("kinds") or {}).get(iid, info.get("broker_kind")),
                    "broker": live.get("broker", info.get("broker")),
                    "halted": live.get("halted"),
                    "retired": live.get("retired") or None,
                    "params": live.get("params", info.get("params")),
                    "edited_on_dashboard": live.get("edited") or info.get("edited") or [],
                    "pnl": round(s.get("pnl") or 0.0, 2),
                    "pnl_by_kind": (snap.get("pnl_kinds") or {}).get(iid),
                    "fills": s.get("fills"),
                    "wins": s.get("wins"),
                    "losses": s.get("losses"),
                    "stops": (stats.get(iid) or {}).get("stops"),
                    "twin_of": info.get("twin_of"),
                    "code": info.get("code"),
                }
            )
        return {
            "run": run,
            "running": bool(session),
            "real_money": snap.get("real"),
            "confirm_needed": snap.get("needs_confirm"),
            "stats": snap.get("stats"),
            "open_positions": len(snap.get("positions") or []),
            "brokers": session.get("brokers"),
            "can_go_live": session.get("can_arm"),
            "strategies": strategies,
        }

    @mcp.tool(annotations=READ)
    def rank_strategies(run: str) -> list[dict[str, Any]]:
        """Every strategy in a run ranked for promotion: trades, win and stop rates, P&L, mean per
        trade (± ~2 standard errors) and `lower`, what it likely makes per trade at worst (the win
        rate's lower ~95% bound times its average win, the rest at its average loss, or a position's
        whole cost before any loss; None under 5 trades). Rank by `lower`, not by P&L."""
        return list(dash.get(f"/runs/{run}/variants"))

    @mcp.tool(annotations=READ)
    def trades(run: str, instance: str = "", outcome: str = "", limit: int = 50) -> dict[str, Any]:
        """A run's trades, newest first, with a summary: what each bought, what it cost and how it
        ended. instance: one strategy. outcome: open, won, lost, stopped or sold."""
        got = dash.get(f"/runs/{run}/trades", instance=instance, outcome=outcome, limit=min(limit, 500))
        names = got.get("names") or {}
        rows = [
            {**t, "market": (names.get(t.get("market_id")) or {}).get("title")} for t in got.get("rows", [])
        ]
        return {"summary": got.get("summary"), "trades": rows}

    @mcp.tool(annotations=READ)
    def health() -> dict[str, Any]:
        """What `quanty doctor` checks where the dashboard runs: config, keys, clock, venues, rate-limit
        headroom, orders left unresolved, disk; each problem with its fix."""
        return dict(dash.get("/health"))

    @mcp.tool(annotations=CHANGE)
    def backtest_variants(
        run: str, instance: str, grid: dict[str, list[Any]], hours: float = 24
    ) -> dict[str, Any]:
        """Backtest one of a run's strategies, as it runs now and one copy per combination of `grid`
        ({param: [values to try]}, at most 50 combinations), on the last `hours` of the dashboard's
        own recordings: the server's real markets, with simulated fills. It runs in the background,
        one at a time, at low priority, and changes nothing in the run. Follow it with
        backtest_status; when done, rank_strategies on its result run compares the variants."""
        return dict(dash.post(f"/runs/{run}/backtest", {"instance": instance, "grid": grid, "hours": hours}))

    @mcp.tool(annotations=READ)
    def backtest_status() -> dict[str, Any] | None:
        """The backtest started last on this dashboard: running, done (`result` is its run, e.g.
        backtest/main-fav_90-20261026-101500) or failed (`error` says why). None if none was started."""
        got = dash.get("/backtest")
        return dict(got) if got else None

    @mcp.tool(annotations=CHANGE)
    def edit_params(run: str, instance: str, params: dict[str, Any], confirm: str = "") -> str:
        """Change a running strategy's params (e.g. {"size": 5}); the strategy validates them, and
        caps and limits still bound them. Kept with the run until reset or saved to the file."""
        return dash.command(run, "params", instance, params, confirm)

    @mcp.tool(annotations=CHANGE)
    def save_params_to_file(run: str, instance: str, confirm: str = "") -> str:
        """Write a strategy's dashboard edits into its experiment file (where the session can write it;
        on a server, `quanty pull` does this instead)."""
        return dash.command(run, "save_params", instance, None, confirm)

    @mcp.tool(annotations=CHANGE)
    def halt(run: str, instance: str = "", confirm: str = "") -> str:
        """Stop a strategy buying (resting buys canceled; exits and stops still go through). Without
        instance, every strategy in the run (the kill switch)."""
        return dash.command(run, "halt" if instance else "kill", instance, None, confirm)

    @mcp.tool(annotations=CHANGE)
    def resume(run: str, instance: str, confirm: str = "") -> str:
        """Let a halted strategy buy again."""
        return dash.command(run, "resume", instance, None, confirm)

    @mcp.tool(annotations=CHANGE)
    def switch_broker(run: str, instance: str, to: str, confirm: str = "") -> str:
        """Move a strategy to sim (paper), shadow (orders checked by the venue) or live (real money),
        from the next market it enters; markets it's in finish where they are. Going live needs a
        session started with `quanty live --arm` and the live broker's caps, and starts a paper twin
        beside it. Moving to live is a real-money decision: only on the user's explicit request."""
        if to not in ("sim", "shadow", "live"):
            raise ToolError("to is sim, shadow or live")
        return dash.command(run, "switch", instance, {"to": to, "twin": True}, confirm)

    @mcp.tool(annotations=CHANGE)
    def reload_experiment(run: str, confirm: str = "") -> str:
        """Bring a running session in line with its experiment file: start strategies added there,
        retire removed ones, apply changed params, brokers and live caps."""
        return dash.command(run, "reload", "", None, confirm)

    for fn in (edit_params, save_params_to_file, halt, resume, switch_broker, reload_experiment):
        fn.__doc__ = f"{fn.__doc__} {CONFIRM}"
    return mcp


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def tunnel(host: str, remote_port: int) -> tuple[subprocess.Popen[bytes], str]:
    """An SSH tunnel to a server's dashboard (it listens on the server's 127.0.0.1)."""
    port = _free_port()
    proc = subprocess.Popen(
        ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-L", f"{port}:127.0.0.1:{remote_port}", host],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,  # stdout is the MCP channel
        stderr=sys.stderr,
    )
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError(f"ssh {host} exited {proc.returncode}: no tunnel to its dashboard")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, f"http://127.0.0.1:{port}"
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"ssh {host}: the tunnel didn't open")


def run(url: str, ssh: str | None, remote_port: int) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # a line per request otherwise
    proc = None
    if ssh:
        proc, url = tunnel(ssh, remote_port)
    try:
        server(Dashboard(url, os.environ.get("QUANTY_DASHBOARD_PASSWORD"))).run("stdio")
    finally:
        if proc is not None:
            proc.terminate()
