import threading
import time
from pathlib import Path

import pytest
from helpers import Hooks
from pydantic import Field

from quanty.brokers.live import LiveBroker
from quanty.brokers.sim import SimBroker
from quanty.control import Controller, ControlStore
from quanty.core import Engine, Instance, Ledger
from quanty.core.engine import Context
from quanty.core.strategy import Params, Strategy

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
    assert state["state"]["instances"]["a"] == {
        "halted": True,
        "broker": "sim",
        "blocked": 0,
        "params": {},
        "edited": [],
    }


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


def token(client: TestClient) -> str:
    html = client.get("/").text
    return str(html.split('name="quanty-token" content="')[1].split('"')[0])


URL = "/api/runs/live/acct/control"


def post(client: TestClient, body: dict[str, object], tok: str | None = None, **headers: str) -> object:
    return client.post(
        URL, json=body, headers={"x-quanty-token": tok if tok is not None else token(client), **headers}
    )


def test_session_state_is_in_the_snapshot(ui: tuple[TestClient, Controller, Path]) -> None:
    client, _, _ = ui
    s = client.get("/api/runs/live/acct").json()["session"]
    assert set(s["instances"]) == {"a", "b"} and s["brokers"]["real"]["kind"] == "live" and s["can_arm"]
    assert "params" in s["actions"]  # the dashboard offers Edit only when the session can do it
    assert "save" in s  # and Save to file / Copy for the file by how this session can save


def test_ui_gates(ui: tuple[TestClient, Controller, Path]) -> None:
    client, _, tmp = ui
    assert post(client, {"action": "kill", "confirm": "acct"}, tok="wrong").status_code == 403  # type: ignore[attr-defined]
    cross = post(client, {"action": "kill", "confirm": "acct"}, origin="https://evil.example")
    assert cross.status_code == 403  # type: ignore[attr-defined]
    r = post(client, {"action": "kill"})
    assert r.status_code == 400 and "type acct to confirm" in r.json()["detail"]  # type: ignore[attr-defined]
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
        r = post(client, {"action": "halt", "target": "a", "confirm": "acct"})
    finally:
        stop.set()
        t.join()
    assert r.json() == {"ok": True, "result": "halted a"}  # type: ignore[attr-defined]
    assert c.trader.halted == {"a"}
    assert client.get("/api/runs/live/acct").json()["commands"][0]["result"] == "halted a"


def test_not_running_session_gets_no_command(ui: tuple[TestClient, Controller, Path]) -> None:
    client, _, tmp = ui
    store = ControlStore(tmp / "control.sqlite")
    with store.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE sessions SET heartbeat_ts = 0")
    r = post(client, {"action": "kill", "confirm": "acct"})
    assert r.status_code == 409 and store.history("live/acct") == []  # type: ignore[attr-defined]


def test_demo_speed_switches_and_real_sessions_refuse_it(tmp_path: Path) -> None:
    from quanty.control import Playback

    c, store, _ = make(tmp_path)
    assert run_cmd(c, store, "speed", "1")["status"] == "failed"  # a real session has no playback
    c.playback = Playback(10.0, (1.0, 10.0))
    r = run_cmd(c, store, "speed", "1")
    assert r["status"] == "done" and r["result"] == "real time" and c.playback.speed == 1.0
    assert run_cmd(c, store, "speed", "3")["status"] == "failed" and c.playback.speed == 1.0
    session = store.session("live/acct")
    assert session is not None
    assert session["state"]["speed"] == {
        "value": 1.0,
        "options": [1.0, 10.0],
        "catching_up": False,
        "progress": 0.0,
        "seed": None,
        "new_day": False,
    }


def test_playback_reanchors_without_jumping() -> None:
    import asyncio

    from quanty.control import Playback

    async def go() -> list[float]:
        pb = Playback(1000.0, (1.0, 1000.0))
        t0 = time.monotonic()
        await pb.wait_for(0)
        await pb.wait_for(200_000)  # 200 s of events at 1000x: 0.2 s
        fast = time.monotonic() - t0
        pb.set(100.0)
        t1 = time.monotonic()
        await pb.wait_for(200_000)  # the switch happens here: no catch-up wait
        await pb.wait_for(210_000)  # 10 s at 100x: 0.1 s
        return [fast, time.monotonic() - t1]

    fast, after = asyncio.run(go())
    assert 0.15 < fast < 0.4 and 0.07 < after < 0.3


def test_new_day_only_in_demos(tmp_path: Path) -> None:
    from quanty.control import Playback

    c, store, _ = make(tmp_path)
    assert "only demos" in str(run_cmd(c, store, "new_day")["result"])
    c.playback = Playback(10.0, (1.0, 10.0))
    c.playback.on_new_day = lambda: 4242
    r = run_cmd(c, store, "new_day")
    assert r["status"] == "done" and r["result"] == "started a new day (seed 4242)"


class SizeParams(Params):
    size: float = Field(10, gt=0, le=100, description="contracts per order")
    side: str = "yes"


class Sized(Strategy[SizeParams]):
    params_type = SizeParams

    def __init__(self, params: SizeParams) -> None:
        super().__init__(params)
        self.seen: list[tuple[float, float]] = []

    def on_params(self, ctx: Context, old: SizeParams) -> None:
        self.seen.append((old.size, self.params.size))


def sized(tmp: Path) -> tuple[Controller, ControlStore, Engine]:
    insts = [Instance("s", Sized(SizeParams(size=5)), SimBroker("sim"))]
    engine = Engine(insts, ledger=Ledger(tmp / "l.sqlite", durable=True), clock_ms=None, strict=False)
    store = ControlStore(tmp / "control.sqlite")
    return Controller(store, "paper/acct", engine, can_arm=False), store, engine


def params_cmd(c: Controller, store: ControlStore, action: str, value: object = None) -> dict[str, object]:
    cid = store.submit("paper/acct", action, "s", value)
    c.tick()
    return next(r for r in store.history("paper/acct") if r["id"] == cid)


def test_params_change_while_running_and_survive_a_restart(tmp_path: Path) -> None:
    c, store, engine = sized(tmp_path)
    strat = engine.instances["s"].strategy
    assert isinstance(strat, Sized)
    r = params_cmd(c, store, "params", {"size": 25})
    assert r["status"] == "done" and r["result"] == "s: size 5.0 → 25.0"
    assert strat.params.size == 25 and strat.seen == [(5, 25)]  # the strategy was told
    state = store.session("paper/acct")
    assert state is not None and state["state"]["instances"]["s"]["edited"] == ["size"]
    for bad in ({"size": 0}, {"size": "big"}, {"leverage": 3}):
        r = params_cmd(c, store, "params", bad)
        assert r["status"] == "failed" and strat.params.size == 25, r  # refused; nothing changed
    assert "no param leverage" in str(params_cmd(c, store, "params", {"leverage": 3})["result"])

    c2, _, engine2 = sized(tmp_path)  # a restarted session: same run, fresh engine from the file
    assert engine2.instances["s"].strategy.params.size == 25 and c2.edited("s") == {"size": 25}
    r = params_cmd(c2, store, "reset_params")
    assert r["status"] == "done" and engine2.instances["s"].strategy.params.size == 5
    assert store.overrides("paper/acct") == {}


def test_saved_changes_that_no_longer_fit_are_dropped(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "control.sqlite")
    store.set_override("paper/acct", "s", {"size": 25, "removed_param": 1})
    _, _, engine = sized(tmp_path)
    assert engine.instances["s"].strategy.params.size == 5 and store.overrides("paper/acct") == {}


def test_edit_from_the_dashboard(tmp_path: Path) -> None:
    (tmp_path / "paper").mkdir()
    c, _, engine = sized(tmp_path)
    Ledger(tmp_path / "paper" / "acct.sqlite").close()
    c.tick()
    client = TestClient(create_app(tmp_path))
    stop = threading.Event()

    def session() -> None:
        while not stop.is_set():
            c.tick()
            time.sleep(0.05)

    t = threading.Thread(target=session)
    t.start()
    try:
        r = client.post(
            "/api/runs/paper/acct/control",
            json={"action": "params", "target": "s", "value": {"size": 12.5}},
            headers={"x-quanty-token": token(client)},
        )
    finally:
        stop.set()
        t.join()
    assert r.json() == {"ok": True, "result": "s: size 5.0 → 12.5"}
    assert engine.instances["s"].strategy.params.size == 12.5


def test_a_session_older_than_the_dashboard_is_told_to_restart(
    ui: tuple[TestClient, Controller, Path],
) -> None:
    client, c, tmp = ui
    store = ControlStore(tmp / "control.sqlite")
    state = c.state()
    del state["actions"]  # what a session from before actions were listed publishes
    store.heartbeat("live/acct", c.started_ts, True, state)
    r = post(client, {"action": "params", "target": "a", "value": {"x": 1}, "confirm": "acct"})
    assert r.status_code == 409 and "restart it" in r.json()["detail"]  # type: ignore[attr-defined]
    assert store.history("live/acct") == []  # never queued


def test_save_params_writes_the_experiment_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tomllib

    monkeypatch.delenv("QUANTY_IN_IMAGE", raising=False)
    exp = tmp_path / "main.toml"
    exp.write_text('# my experiment\n[[instances]]\nid = "s"  # the only one\nstrategy = "sized"\n')
    c, store, _ = sized(tmp_path)
    assert c.save_mode is None  # no file: a demo can't save
    c.experiment = exp
    assert c.save_mode == "file"
    params_cmd(c, store, "params", {"size": 25})
    r = params_cmd(c, store, "save_params")
    assert r["status"] == "done" and "saved size = 25.0 to main.toml" in str(r["result"])
    text = exp.read_text()
    assert "# my experiment" in text and "# the only one" in text  # comments kept
    assert tomllib.loads(text)["instances"][0]["params"] == {"size": 25.0}
    assert c.edited("s") == {} and store.overrides("paper/acct") == {}  # the file is the source again
    assert "nothing changed" in str(params_cmd(c, store, "save_params")["result"])

    monkeypatch.setenv("QUANTY_IN_IMAGE", "1")  # a container: the image's copy is replaced on deploy
    params_cmd(c, store, "params", {"size": 30})
    assert c.save_mode == "copy"
    r = params_cmd(c, store, "save_params")
    assert r["status"] == "failed" and "copy the lines" in str(r["result"])
    assert tomllib.loads(exp.read_text())["instances"][0]["params"]["size"] == 25.0
