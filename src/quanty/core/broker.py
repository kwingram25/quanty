from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from quanty.core.events import BrokerEvent, Event
from quanty.core.orders import Order

if TYPE_CHECKING:
    from quanty.core.engine import Engine


class Broker(ABC):
    """Turns orders into fills: simulated (backtest, paper), previewed (shadow) or real (live).

    Results go back to the engine as Fill and OrderUpdate events via emit(). A broker never
    mutates an Order; the engine does, from those events. Fills for an order are emitted before
    the OrderUpdate that reports it done.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._engine: Engine | None = None

    def attach(self, engine: "Engine") -> None:
        self._engine = engine

    @property
    def engine(self) -> "Engine":
        if self._engine is None:
            raise RuntimeError(f"broker {self.name} is not attached to an engine")
        return self._engine

    def emit(self, event: Event) -> None:
        self.engine.schedule(event)

    def on_event(self, event: Event) -> None:  # noqa: B027 - optional hook
        """Market data and lifecycle events, seen before strategies see them."""

    @abstractmethod
    def submit(self, order: Order) -> None: ...

    @abstractmethod
    def cancel(self, order: Order) -> None: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Finish in-flight venue requests and close connections (sessions call this on shutdown)."""

    def handle(self, event: BrokerEvent) -> None:
        """This broker's own scheduled events."""
        raise NotImplementedError(f"{type(self).__name__} schedules no events")
