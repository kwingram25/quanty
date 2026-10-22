"""User config, read from $QUANTY_CONFIG or ~/.config/quanty/config.toml.

The config lives outside the repo so private strategy paths and host details are never committed.

    strategy_paths = ["~/code/my-strategies/strategies"]
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("~/.config/quanty/config.toml")


@dataclass(frozen=True)
class Config:
    strategy_paths: list[Path] = field(default_factory=list)
    source: Path | None = None


def config_path() -> Path:
    return Path(os.environ.get("QUANTY_CONFIG", DEFAULT_PATH)).expanduser()


def load(path: Path | None = None) -> Config:
    """Load the config; a missing file gives the defaults (no private strategies)."""
    path = path or config_path()
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())
    paths = raw.get("strategy_paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError(f"{path}: strategy_paths must be a list of strings")
    # relative paths are relative to the config file, not the working directory
    resolved = [(path.parent / Path(p).expanduser()).resolve() for p in paths]
    return Config(strategy_paths=resolved, source=path)
