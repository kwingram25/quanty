"""Strategy template: copy to strategies/<name>.py and edit the placeholders.

The engine's Strategy base class arrives in step 1; until then this file shows the intended
shape: tunable params, the series it trades, a fair-value model, and a decision hook.
"""

from dataclasses import dataclass

DESCRIPTION = "TODO: one line on what this strategy does"

# TODO: the repeated markets this strategy trades, as "<venue>:<series>"
SERIES = ["kalshi:TODO_SERIES"]


@dataclass(frozen=True)
class Params:
    """Knobs an experiment config can override. TODO: replace with your own."""

    min_edge: float = 0.02  # buy when fair value beats the price by at least this much
    max_price: float = 0.95  # never pay more than this
    size: int = 1  # contracts per order
    enter_last_s: int = 300  # only trade in the last N seconds of a window


def fair(features: dict[str, float]) -> float:
    """TODO: your model. Return the probability that YES settles true."""
    raise NotImplementedError


def decide(params: Params, fair_yes: float, best_ask_yes: float, seconds_left: int) -> int:
    """TODO: your rule. Return contracts of YES to buy now (0 = do nothing)."""
    if seconds_left > params.enter_last_s or best_ask_yes > params.max_price:
        return 0
    return params.size if fair_yes - best_ask_yes >= params.min_edge else 0
