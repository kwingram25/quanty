from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from quanty import cli
from quanty.ui.app import create_app
from quanty.ui.auth import Auth, hash_password, verify_password

HASH = hash_password("correct horse")


def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path, password_hash=HASH), follow_redirects=False)


def test_hashes() -> None:
    assert verify_password("correct horse", HASH) and not verify_password("wrong", HASH)
    assert not verify_password("x", "garbage") and hash_password("a") != hash_password("a")  # salted
    with pytest.raises(ValueError, match="hash from `quanty password`"):
        Auth("correct horse")  # the password itself in .env is refused


def test_everything_needs_a_session(tmp_path: Path) -> None:
    c = client(tmp_path)
    r = c.get("/runs/paper/x")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/runs/paper/x"
    assert c.get("/api/runs").status_code == 401
    assert c.get("/login").status_code == 200 and 'type="password"' in c.get("/login").text
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/api/runs/paper/x/stream") as ws:
        ws.receive_json()


def test_sign_in_and_out(tmp_path: Path) -> None:
    c = client(tmp_path)
    bad = c.post("/login", data={"password": "nope", "next": "/feeds"})
    assert bad.status_code == 200 and "Wrong password" in bad.text and c.get("/api/runs").status_code == 401
    assert 'name="next" value="/feeds"' in bad.text  # still heading to the page asked for
    ok = c.post("/login", data={"password": "correct horse", "next": "/feeds"})
    assert ok.status_code == 303 and ok.headers["location"] == "/feeds"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/runs").status_code == 200
    page = c.get("/")
    assert page.status_code == 200 and 'name="quanty-auth"' in page.text  # the app offers "Sign out"
    assert c.post("/logout").status_code == 303 and c.get("/api/runs").status_code == 401


def test_lockout_and_no_open_redirect(tmp_path: Path) -> None:
    c = client(tmp_path)
    for _ in range(5):
        c.post("/login", data={"password": "nope"})
    locked = c.post("/login", data={"password": "correct horse"})
    assert "Too many attempts" in locked.text  # even the right password waits out the lockout
    c2 = client(tmp_path / "other")
    r = c2.post("/login", data={"password": "correct horse", "next": "//evil.example/x"})
    assert r.headers["location"] == "/"


def test_no_password_means_no_login(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path, password_hash=None))
    assert (
        c.get("/api/runs").status_code == 200 and c.get("/login").status_code == 200
    )  # the app's own 404 page


def test_password_command_writes_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("KALSHI_KEY_ID=abc\nQUANTY_UI_PASSWORD=old\n")
    res = CliRunner().invoke(cli.app, ["password", "--write"], input="long enough pw\nlong enough pw\n")
    assert res.exit_code == 0, res.output
    lines = (tmp_path / ".env").read_text().splitlines()
    assert (
        lines[0] == "KALSHI_KEY_ID=abc"
        and len([x for x in lines if x.startswith("QUANTY_UI_PASSWORD=")]) == 1
    )
    value = lines[-1].split("=", 1)[1]
    assert value.startswith("'") and value.endswith("'")  # quoted: Compose would substitute its `$`s
    assert verify_password("long enough pw", value.strip("'"))
    short = CliRunner().invoke(cli.app, ["password"], input="short\nshort\n")
    assert short.exit_code == 2
