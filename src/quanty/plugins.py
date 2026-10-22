"""Discover strategy modules from the bundled examples and the user's strategy_paths.

Every non-underscore *.py file in a search path is a strategy module, named after its file.
A user module shadows a bundled example of the same name.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

EXAMPLES = Path(__file__).parent / "strategies"


def _module_files(path: Path) -> list[Path]:
    if not path.is_dir():
        raise FileNotFoundError(f"strategy path is not a directory: {path}")
    return sorted(p for p in path.glob("*.py") if not p.name.startswith("_"))


def _import(file: Path) -> ModuleType:
    name = f"quanty_strategies.{file.stem}"
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load strategy module {file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def discover(paths: list[Path], include_examples: bool = True) -> dict[str, ModuleType]:
    """Map strategy name -> module; later paths shadow earlier ones."""
    search = ([EXAMPLES] if include_examples else []) + paths
    files: dict[str, Path] = {}
    for path in search:
        for file in _module_files(path):
            files[file.stem] = file
    return {name: _import(file) for name, file in files.items()}
