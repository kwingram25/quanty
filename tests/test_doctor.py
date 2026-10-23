import asyncio
import json
import os
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from quanty import cli, doctor
from quanty.init import Answers, create
from quanty.ui.auth import hash_password
from quanty.venues.orders import KalshiOrders, OrderAPI, PolymarketOrders


class FakeSigner:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {}


def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY",
                "QUANTY_UI_PASSWORD"):  # fmt: skip
        monkeypatch.delenv(var, raising=False)
    proj = tmp_path / "proj"
    create(proj, Answers(strategy="edge_finder", venues=["kalshi"]), vendor=False)
    monkeypatch.chdir(proj)
    return proj


def checks(proj: Path) -> dict[str, doctor.Check]:
    found = doctor.run(proj, sorted((proj / "experiments").glob("*.toml")), proj / "feeds.toml",
                       proj / "data", offline=True)  # fmt: skip
    return {c.name: c for c in found}


def test_a_new_project_only_lacks_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = project(tmp_path, monkeypatch)
    got = checks(proj)
    assert got["strategies"].status == "ok" and "edge_finder" in got["strategies"].detail
    assert got["experiments/main.toml"].status == "ok" and got["feeds.toml"].detail == "kalshi"
    assert got["Kalshi"].status == "fail" and "KALSHI_KEY_ID" in got["Kalshi"].detail
    assert "Polymarket US" not in got  # not configured, so not required
    assert got["previous run"].status == "ok" and got["network"].status == "skip"
    res = CliRunner().invoke(cli.app, ["doctor", "--offline"])
    assert res.exit_code == 1 and "✗ Kalshi" in res.output and "fix: add the key to .env" in res.output


def test_config_and_key_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = project(tmp_path, monkeypatch)
    (proj / "experiments" / "bad.toml").write_text('[[instance]]\nstrategy = "nope"\n')
    env = proj / ".env"
    env.write_text("QUANTY_UI_PASSWORD=hunter2\n")
    os.chmod(env, 0o644)
    got = checks(proj)
    assert got["experiments/bad.toml"].status == "fail"
    assert got[".env"].status == "warn" and got[".env"].fix == "chmod 600 .env"
    assert got["dashboard login"].status == "fail"  # the password itself, not a hash
    monkeypatch.setenv("QUANTY_UI_PASSWORD", hash_password("long enough"))
    assert checks(proj)["dashboard login"].detail == "on"


def test_unresolved_live_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = project(tmp_path, monkeypatch)
    journal = proj / "data" / "live" / "main.live.orders.jsonl"
    journal.parent.mkdir(parents=True)
    rows = [{"order": "a"}, {"order": "a", "done": True}, {"order": "b"}]
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    got = checks(proj)["previous run"]
    assert got.status == "warn" and got.detail.startswith("1 order(s)")


def venue_checks(handler: httpx.MockTransport) -> dict[str, doctor.Check]:
    apis: dict[str, OrderAPI] = {
        "kalshi": KalshiOrders(FakeSigner()),
        "polymarket_us": PolymarketOrders(FakeSigner()),
    }
    found = asyncio.run(doctor._venues({"kalshi", "polymarket_us"}, apis, handler))
    return {c.name: c for c in found}


def test_venues_keys_clock_and_limits() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        now = formatdate(usegmt=True)
        if req.url.host.endswith("kalshi.com") or "kalshi" in req.url.host:
            if req.url.path.endswith("/portfolio/balance"):
                return httpx.Response(200, json={"balance": 12345}, headers={"date": now})
            if req.url.path.endswith("/account/limits"):
                lim = {"usage_tier": "basic", "read": {"refill_rate": 200}, "write": {"refill_rate": 100}}
                return httpx.Response(200, json=lim)
            return httpx.Response(200, headers={"date": now})
        if req.url.path.endswith("/account/balances"):
            return httpx.Response(401, json={"message": "bad key"})
        return httpx.Response(200, headers={"date": formatdate(0, usegmt=True)})  # a clock decades off

    got = venue_checks(httpx.MockTransport(handle))
    assert got["Kalshi"].status == "ok" and "$123.45" in got["Kalshi"].detail
    assert got["Kalshi clock"].status == "ok"
    assert got["Kalshi rate limit"].detail.startswith("tier basic: reads 200/s, writes 100/s")
    assert got["Polymarket US"].status == "fail" and "rejected" in got["Polymarket US"].detail
    assert got["Polymarket US clock"].status == "fail"


def test_rate_limited_and_unreachable() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if "kalshi" in req.url.host:
            return httpx.Response(429, json={"error": "too many requests"})
        raise httpx.ConnectError("no route")

    got = venue_checks(httpx.MockTransport(handle))
    assert got["Kalshi"].status == "warn" and "rate limited" in got["Kalshi"].detail
    assert got["Polymarket US"].status == "fail" and "unreachable" in got["Polymarket US"].detail


def test_a_key_file_path_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = project(tmp_path, monkeypatch)
    monkeypatch.setenv("KALSHI_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "gone.pem"))
    got = checks(proj)["Kalshi"]
    assert got.status == "fail" and "KALSHI_SECRET_KEY" in got.fix


def test_polymarket_only_projects_are_told_where_the_index_comes_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = project(tmp_path, monkeypatch)
    (proj / "feeds.toml").write_text(
        '[feeds.polymarket_us]\nseries = [{ name = "btc_updown_15m", slug_prefix = "cpc-btc-updown-15m", '
        "period_s = 900 }]\n"
    )
    got = checks(proj)["index data"]
    assert got.status == "warn" and 'index_ids = ["BRTI"]' in got.fix
    (proj / "feeds.toml").write_text(
        (proj / "feeds.toml").read_text() + '[feeds.kalshi]\nseries = ["KXBTC15M"]\nindex_ids = ["BRTI"]\n'
    )
    assert "index data" not in checks(proj)


def test_recording_without_keys_stops_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quanty.venues.auth import keys

    project(tmp_path, monkeypatch)  # a Kalshi project, no keys anywhere
    keys.cache_clear()
    try:
        res = CliRunner().invoke(
            cli.app, ["record", "--config", "feeds.toml", "--data", "data", "--duration", "5"]
        )
    finally:
        keys.cache_clear()
    assert res.exit_code == 2 and "kalshi: KALSHI_KEY_ID is not set" in res.output, res.output
