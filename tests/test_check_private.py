import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import check_private


def test_flags_matching_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("# comment\nsecret_strat\n10\\.0\\.0\\.\\d+\n")
    clean = tmp_path / "clean.py"
    clean.write_text("print('hi')\n")
    dirty = tmp_path / "dirty.py"
    dirty.write_text("ok\nuse secret_strat here\nhost = '10.0.0.7'\n")
    monkeypatch.setenv("QUANTY_DENYLIST", str(deny))
    assert check_private.main([str(clean)]) == 0
    assert check_private.main([str(dirty)]) == 1
    assert len(check_private.scan([dirty], check_private.load_patterns(deny))) == 2


def test_passes_without_denylist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTY_DENYLIST", str(tmp_path / "missing.txt"))
    assert check_private.main([__file__]) == 0
