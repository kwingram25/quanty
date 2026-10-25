"""Helpers that build on the engine for sessions: a paper twin of a running instance."""

from quanty.core.broker import Broker
from quanty.core.engine import Engine, Instance


def twin_of(engine: Engine, inst: Instance, broker: Broker) -> Instance:
    """Start a paper copy of `inst` on `broker` in a running session: the same strategy class,
    params, series and limits, marked twin_of it (the dashboard compares the two)."""
    taken = set(engine.instances)
    name, n = f"{inst.id}_paper", 2
    while name in taken:
        name, n = f"{inst.id}_paper{n}", n + 1
    strategy = type(inst.strategy)(inst.strategy.params)
    strategy.series = inst.strategy.series
    twin = Instance(name, strategy, broker, inst.limits, twin_of=inst.id)
    engine.add_instance(twin)
    return twin
