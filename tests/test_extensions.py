"""Feeds, venues and signals from extensions: the project's extensions/ folder and installed packages."""

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from quanty import doctor, experiment, extensions, feeds, signals
from quanty.core.events import Observation
from quanty.feeds.base import Raw
from quanty.venues.orders import default_apis

EXTENSION = """
from typing import ClassVar, Literal

import httpx
import orjson

from quanty.core.events import Event, Observation
from quanty.feeds.base import FeedConfig, PollingConfig, PollingFeed, Raw
from quanty.signals import Signal, SignalConfig, price_of
from quanty.venues.orders import AccountBalance, OrderAPI


class StationConfig(PollingConfig):
    kind: Literal["station"] = "station"
    station: str = "KNYC"


class Station(PollingFeed):
    kind = "station"
    config_type: ClassVar[type[FeedConfig]] = StationConfig
    config: StationConfig

    def parse(self, raw: Raw) -> list[Event]:
        msg = orjson.loads(raw.text)
        return [Observation(ts=raw.t, key=f"station:{self.config.station}", value=msg["temp"], unit="F")]


class DoubleConfig(SignalConfig):
    kind: Literal["double"] = "double"
    by: float = 2


class Double(Signal):
    config_type = DoubleConfig

    def update(self, ev):
        p = price_of(ev)
        self.value = None if p is None else p * self.config.by

    def __init__(self, config: DoubleConfig) -> None:
        super().__init__(config)
        self.config = config


class Exchange(OrderAPI):
    venue = "exchange"
    label = "Example Exchange"
    base_url = "https://exchange.test"

    @property
    def signer(self):
        return self

    def headers(self, method, path):
        return {}

    def place_request(self, order, market): raise NotImplementedError
    async def preview(self, client, order, market): raise NotImplementedError
    async def place(self, client, req): raise NotImplementedError
    async def get(self, client, venue_id, market_id): raise NotImplementedError
    async def cancel(self, client, venue_id, market_id): raise NotImplementedError

    async def balance(self, client):
        res = await self._send(client, "GET", "/balance")
        return AccountBalance(cash=res["cash"], buying_power=res["cash"])

    async def limits(self, client):
        return "10 requests/s"
"""


def _clear() -> None:
    for f in (extensions.modules, feeds.kinds, signals.kinds):
        f.cache_clear()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(extensions, "entry_points", lambda group: [])
    _clear()
    yield
    for name in [m for m in sys.modules if m.startswith("quanty_extensions.")]:
        del sys.modules[name]
    _clear()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "extensions").mkdir()
    (tmp_path / "extensions" / "station.py").write_text(EXTENSION)
    (tmp_path / "extensions" / "_helpers.py").write_text("raise RuntimeError('not loaded')\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_projects_feed_venue_and_signal_are_found(project: Path) -> None:
    feed = feeds.make("nyc", {"kind": "station", "station": "KLGA", "interval_s": 300})
    (ev,) = feed.parse(Raw("nyc", 1000, '{"temp": 61.5}'))
    assert isinstance(ev, Observation) and (ev.key, ev.value) == ("station:KLGA", 61.5)
    assert {"kalshi", "polymarket_us", "exchange"} <= set(default_apis())
    assert {"last", "double"} <= set(signals.kinds())


def test_an_experiment_takes_an_extension_signal(project: Path) -> None:
    raw = {
        "instances": [{"id": "a", "strategy": "s"}],
        "signals": [{"kind": "double", "name": "d", "key": "k"}],
    }
    exp = experiment.parse(raw)
    (sig,) = experiment.signals(exp)
    sig.update(Observation(ts=1, key="k", value=3.0))
    assert sig.value == 6.0
    assert exp.model_dump(mode="json")["signals"] == [{"name": "d", "key": "k", "kind": "double", "by": 2.0}]
    with pytest.raises(ValueError, match=r"signals\.0\.kind: unknown signal kind 'triple'"):
        experiment.parse({**raw, "signals": [{"kind": "triple", "name": "d", "key": "k"}]})


def test_doctor_checks_an_extension_venue(project: Path) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cash": 12.5}, headers={"date": "Tue, 27 Oct 2026 06:00:00 GMT"})

    got = asyncio.run(doctor._venues({"exchange"}, None, httpx.MockTransport(handle)))
    by = {c.name: c for c in got}
    assert by["Example Exchange"].status == "ok" and "$12.50" in by["Example Exchange"].detail
    assert by["Example Exchange rate limit"].detail == "10 requests/s"


def test_a_name_defined_twice_is_an_error(project: Path) -> None:
    (project / "extensions" / "copy.py").write_text(
        "from quanty.feeds.coinbase import Coinbase\n\nclass Mine(Coinbase):\n    kind = 'coinbase'\n"
    )
    with pytest.raises(ValueError, match="feed kind 'coinbase' is defined twice"):
        feeds.kinds()


def test_a_broken_extension_names_its_file(project: Path) -> None:
    (project / "extensions" / "broken.py").write_text("import nothing_here\n")
    with pytest.raises(ImportError, match=r"extensions/broken\.py: ModuleNotFoundError"):
        extensions.modules()


def test_installed_packages_declare_entry_points(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pkg_station.py").write_text(EXTENSION)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)  # no extensions/ here
    import importlib

    ep = SimpleNamespace(
        name="station", value="pkg_station", load=lambda: importlib.import_module("pkg_station")
    )
    monkeypatch.setattr(extensions, "entry_points", lambda group: [ep] if group == extensions.GROUP else [])
    assert "station" in feeds.kinds() and "exchange" in default_apis()
    sys.modules.pop("pkg_station", None)


def test_no_extensions_is_the_built_ins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert set(feeds.kinds()) == {"coinbase", "binance", "kalshi", "polymarket_us"}
    assert set(default_apis()) == {"kalshi", "polymarket_us"}
