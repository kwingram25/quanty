"""Feeds, venues and signals beyond the built-in ones, found the way strategies are.

An extension is a Python module that defines subclasses of quanty's bases: a `Feed` with a `kind`
(a data source, or a venue's market data), an `OrderAPI` with a `venue` (a venue's orders), a
`Signal` (a value derived from data events). quanty finds them in two places:

- the project's `extensions/` folder: every non-underscore *.py file in it (deployed with the
  project, like `strategies/`);
- installed packages that declare an entry point in the "quanty.extensions" group, pointing at a
  module: `[project.entry-points."quanty.extensions"] my_venue = "my_package.venue"`.

A feed kind, venue or signal kind may be defined once: an extension can't replace a built-in one.
See docs/extending.md.
"""

import importlib.util
import inspect
import sys
from collections.abc import Mapping
from functools import cache
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

GROUP = "quanty.extensions"
LOCAL = Path("extensions")


@cache
def modules() -> tuple[ModuleType, ...]:
    """The extension modules: installed packages' first, then the project's (cached)."""
    out = []
    for ep in entry_points(group=GROUP):
        try:
            module = ep.load()
        except Exception as exc:
            raise ImportError(f"extension {ep.name} ({ep.value}): {type(exc).__name__}: {exc}") from exc
        if not isinstance(module, ModuleType):
            raise ImportError(f"extension {ep.name} ({ep.value}) must name a module, not {module!r}")
        out.append(module)
    if LOCAL.is_dir():
        root = str(LOCAL.resolve().parent)  # the project: extensions can import its helper packages
        if root not in sys.path:
            sys.path.insert(0, root)
        for file in sorted(p for p in LOCAL.glob("*.py") if not p.name.startswith("_")):
            out.append(_import(file))
    return tuple(out)


def _import(file: Path) -> ModuleType:
    name = f"quanty_extensions.{file.stem}"
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load extension {file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ImportError(f"extension {file}: {type(exc).__name__}: {exc}") from exc
    return module


def classes[T](base: type[T]) -> list[type[T]]:
    """Concrete subclasses of base defined in the extension modules (not ones they import)."""
    return [
        obj
        for m in modules()
        for obj in vars(m).values()
        if inspect.isclass(obj)
        and issubclass(obj, base)
        and obj is not base
        and obj.__module__ == m.__name__
        and not inspect.isabstract(obj)
    ]


def register[T](what: str, built_in: Mapping[str, T], extra: list[tuple[str, T]]) -> dict[str, T]:
    """built_in plus extra, by name; a name defined twice is an error naming both."""
    out = dict(built_in)
    for name, value in extra:
        if name in out:
            raise ValueError(
                f"{what} {name!r} is defined twice: by {_origin(out[name])} and {_origin(value)}"
            )
        out[name] = value
    return out


def _origin(value: object) -> str:
    cls = value if inspect.isclass(value) else type(value)
    return f"{cls.__module__}.{cls.__qualname__}"
