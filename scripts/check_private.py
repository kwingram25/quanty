"""Fail if any given file matches a pattern from the private denylist.

The denylist (one regex per line, # comments) lives outside this repo, so the names it guards
are never committed here: $QUANTY_DENYLIST, default ~/.config/quanty/denylist.txt.
Without a denylist the check passes with a warning (contributors don't have one).

    python scripts/check_private.py FILE...
"""

import os
import re
import sys
from pathlib import Path

DEFAULT = Path("~/.config/quanty/denylist.txt")


def load_patterns(path: Path) -> list[re.Pattern[str]]:
    lines = (line.strip() for line in path.read_text().splitlines())
    return [re.compile(line) for line in lines if line and not line.startswith("#")]


def scan(files: list[Path], patterns: list[re.Pattern[str]]) -> list[str]:
    hits = []
    for file in files:
        try:
            text = file.read_text(errors="ignore")
        except (FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            hits += [f"{file}:{n}: matches a private pattern" for p in patterns if p.search(line)]
    return hits


def main(argv: list[str]) -> int:
    denylist = Path(os.environ.get("QUANTY_DENYLIST", DEFAULT)).expanduser()
    if not denylist.exists():
        print(f"check_private: no denylist at {denylist}, skipping", file=sys.stderr)
        return 0
    hits = scan([Path(a) for a in argv], load_patterns(denylist))
    for hit in hits:  # the pattern itself is not printed, so it can't leak into CI logs
        print(hit, file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
