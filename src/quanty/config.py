"""User config, read from $QUANTY_CONFIG or ~/.config/quanty/config.toml.

The config lives outside the repo so private strategy paths and host details are never committed.

    strategy_paths = ["~/code/my-strategies/strategies"]
    python_paths = ["~/code/my-strategies"]   # importable from strategies, e.g. `from models import ...`
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("~/.config/quanty/config.toml")


@dataclass(frozen=True)
class Config:
    strategy_paths: list[Path] = field(default_factory=list)
    python_paths: list[Path] = field(default_factory=list)
    source: Path | None = None


def config_path() -> Path:
    return Path(os.environ.get("QUANTY_CONFIG", DEFAULT_PATH)).expanduser()


def load(path: Path | None = None) -> Config:
    """Load the config; a missing file gives the defaults (no private strategies)."""
    path = path or config_path()
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())
    return Config(
        strategy_paths=_paths(raw, "strategy_paths", path),
        python_paths=_paths(raw, "python_paths", path),
        source=path,
    )


def _paths(raw: dict[str, object], key: str, source: Path) -> list[Path]:
    paths = raw.get(key, [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError(f"{source}: {key} must be a list of strings")
    # relative paths are relative to the config file, not the working directory
    return [(source.parent / Path(p).expanduser()).resolve() for p in paths]
