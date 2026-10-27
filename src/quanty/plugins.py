"""Discover strategy modules from the bundled examples, ./strategies and the user's strategy_paths.

Every non-underscore *.py file in a search path is a strategy module, named after its file.
Search order, later shadowing earlier: bundled examples, strategy_paths from the user config, then
./strategies and its immediate subfolders (e.g. a private repo cloned into strategies/private): a
project's own files win, since they're what it deploys. The folder holding ./strategies (the
project) is importable, so strategies can keep helper packages beside them (`from models import x`).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

EXAMPLES = Path(__file__).parent / "strategies"
LOCAL = Path("strategies")


def _module_files(path: Path) -> list[Path]:
    if not path.is_dir():
        raise FileNotFoundError(f"strategy path is not a directory: {path}")
    return sorted(p for p in path.glob("*.py") if not p.name.startswith("_"))


def _local_paths(local: Path) -> list[Path]:
    if not local.is_dir():
        return []
    subdirs = sorted(p for p in local.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    return [local, *subdirs]


def _import(file: Path) -> ModuleType:
    name = f"quanty_strategies.{file.stem}"
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load strategy module {file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def discover(
    paths: list[Path],
    include_examples: bool = True,
    local: Path | None = LOCAL,
    python_paths: list[Path] | None = None,
) -> dict[str, ModuleType]:
    """Map strategy name -> module; later paths shadow earlier ones. python_paths, and the folder
    holding `local`, are added to sys.path first, so strategies can import their own helper packages
    (the project's own first)."""
    here = [local.resolve().parent] if local is not None and local.is_dir() else []
    for extra in [*(python_paths or ()), *here]:  # the last inserted is searched first
        if str(extra) in sys.path:
            sys.path.remove(str(extra))
        sys.path.insert(0, str(extra))
    search = ([EXAMPLES] if include_examples else []) + paths + (_local_paths(local) if local else [])
    files: dict[str, Path] = {}
    for path in search:
        for file in _module_files(path):
            files[file.stem] = file
    return {name: _import(file) for name, file in files.items()}
