import threading
import time
from pathlib import Path

import pytest
from helpers import Hooks

from quanty.brokers.live import LiveBroker
from quanty.brokers.sim import SimBroker
from quanty.control import Controller, ControlStore
from quanty.core import Engine, Instance, Ledger

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from quanty.ui.app import create_app


def make(tmp: Path, *, can_arm: bool = False) -> tuple[Controller, ControlStore, Engine]:
    sim, live = SimBroker("sim"), LiveBroker("real", armed=can_arm)
    insts = [Instance("a", Hooks(), sim), Instance("b", Hooks(), live)]
    engine = Engine(insts, ledger=Ledger(tmp / "l.sqlite", durable=True), clock_ms=None, strict=False)
    store = ControlStore(tmp / "control.sqlite")
    return Controller(store, "live/acct", engine, can_arm=can_arm), store, engine


def run_cmd(c: Controller, store: ControlStore, action: str, target: str = "") -> dict[str, object]:
    cid = store.submit("live/acct", action, target)
    c.tick()
    return next(r for r in store.history("live/acct") if r["id"] == cid)


def test_actions_apply_to_the_session(tmp_path: Path) -> None:
    c, store, engine = make(tmp_path, can_arm=True)
    assert run_cmd(c, store, "halt", "a")["status"] == "done" and engine.halted == {"a"}
    assert run_cmd(c, store, "resume", "a")["status"] == "done" and engine.halted == set()
    assert run_cmd(c, store, "kill")["status"] == "done" and engine.halted == {"a", "b"}
    broker = engine.instances["b"].broker
    assert isinstance(broker, LiveBroker) and broker.armed
    assert run_cmd(c, store, "disarm", "real")["status"] == "done" and not broker.armed
    assert run_cmd(c, store, "arm", "real")["status"] == "done" and broker.armed
    state = store.session("live/acct")
    assert state is not None and state["state"]["brokers"]["real"] == {
        "kind": "live",
        "armed": True,
        "disabled": "",
    }
    assert state["state"]["instances"]["a"] == {"halted": True, "broker": "sim"}


def test_bad_commands_fail_without_effect(tmp_path: Path) -> None:
    c, store, engine = make(tmp_path, can_arm=False)
    r = run_cmd(c, store, "halt", "nope")
    assert r["status"] == "failed" and "no instance" in str(r["result"]) and engine.halted == set()
    r = run_cmd(c, store, "disarm", "sim")
    assert r["status"] == "failed" and "no live broker" in str(r["result"])
    r = run_cmd(c, store, "arm", "real")
    assert r["status"] == "failed" and "--arm" in str(r["result"])
    broker = engine.instances["b"].broker
    assert isinstance(broker, LiveBroker) and not broker.armed
    with pytest.raises(ValueError, match="unknown action"):
        store.submit("live/acct", "sell_everything")


def test_disabled_broker_cannot_be_rearmed(tmp_path: Path) -> None:
    c, store, engine = make(tmp_path, can_arm=True)
    broker = engine.instances["b"].broker
    assert isinstance(broker, LiveBroker)
    broker.disabled = "order outcome unknown"
    r = run_cmd(c, store, "arm", "real")
    assert r["status"] == "failed" and "restart" in str(r["result"])


@pytest.fixture
def ui(tmp_path: Path) -> tuple[TestClient, Controller, Path]:
    (tmp_path / "live").mkdir()
    c, _, _ = make(tmp_path, can_arm=True)
    Ledger(tmp_path / "live" / "acct.sqlite").close()  # the run's own ledger, so the UI lists it
    c.tick()  # heartbeat: the session is running
    return TestClient(create_app(tmp_path)), c, tmp_path


def form_token(client: TestClient) -> str:
    html = client.get("/runs/live/acct").text
    return str(html.split('name="token" value="')[1].split('"')[0])


def test_controls_shown_only_for_a_running_session(ui: tuple[TestClient, Controller, Path]) -> None:
    client, _, _ = ui
    html = client.get("/runs/live/acct").text
    assert "Kill switch" in html and 'value="halt:a"' in html and 'value="disarm:real"' in html


def test_ui_gates(ui: tuple[TestClient, Controller, Path]) -> None:
    client, _, tmp = ui
    token = form_token(client)
    url = "/runs/live/acct/control"
    assert client.post(url, data={"cmd": "kill:", "token": "wrong", "confirm": "acct"}).status_code == 403
    cross = client.post(url, data={"cmd": "kill:", "token": token, "confirm": "acct"},
                        headers={"origin": "https://evil.example"})  # fmt: skip
    assert cross.status_code == 403
    r = client.post(url, data={"cmd": "kill:", "token": token}, follow_redirects=False)
    assert r.status_code == 303 and "type+acct+to+confirm" in r.headers["location"]
    assert ControlStore(tmp / "control.sqlite").history("live/acct") == []  # nothing was sent


def test_button_press_reaches_the_session(ui: tuple[TestClient, Controller, Path]) -> None:
    client, c, _ = ui
    stop = threading.Event()

    def session() -> None:  # the running session's control loop
        while not stop.is_set():
            c.tick()
            time.sleep(0.05)

    t = threading.Thread(target=session)
    t.start()
    try:
        r = client.post(
            "/runs/live/acct/control", data={"cmd": "halt:a", "token": form_token(client), "confirm": "acct"}
        )
    finally:
        stop.set()
        t.join()
    assert r.status_code == 200 and "halted a" in r.text
    assert c.trader.halted == {"a"}
    assert "Command history" in r.text


def test_not_running_session_gets_no_command(ui: tuple[TestClient, Controller, Path]) -> None:
    client, _, tmp = ui
    token = form_token(client)
    store = ControlStore(tmp / "control.sqlite")
    with store.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE sessions SET heartbeat_ts = 0")
    r = client.post("/runs/live/acct/control", data={"cmd": "kill:", "token": token, "confirm": "acct"},
                    follow_redirects=False)  # fmt: skip
    assert "not+running" in r.headers["location"] and store.history("live/acct") == []
