import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quanty import cli, experiment, plugins
from quanty.init import Answers, create

runner = CliRunner()


def test_init_writes_a_working_project(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    written = create(proj, Answers(strategy="edge_finder", venues=["kalshi", "polymarket_us"]), vendor=False)
    assert {"strategies/edge_finder.py", "experiments/main.toml", "feeds.toml", ".env", "Dockerfile",
            "compose.yaml"} <= set(written)  # fmt: skip
    assert stat.S_IMODE((proj / ".env").stat().st_mode) == 0o600  # keys: owner only
    assert (
        "pyproject.toml" in written
        and 'dependencies = ["quanty-py>=' in (proj / "pyproject.toml").read_text()
    )
    assert ".env" in (proj / ".dockerignore").read_text()  # never baked into the image
    found = plugins.discover([proj / "strategies"], include_examples=False, local=None)
    exp = experiment.load(proj / "experiments" / "main.toml")
    (inst,) = experiment.build(exp, found)
    assert inst.id == "edge_finder" and inst.strategy.series == ("kalshi:KXBTC15M",)
    feeds = (proj / "feeds.toml").read_text()
    assert "[feeds.kalshi]" in feeds and 'slug_prefix = "cpc-btc-updown-15m"' in feeds


def test_init_writes_the_live_caps_it_was_given(tmp_path: Path) -> None:
    create(tmp_path / "a", Answers(), vendor=False)
    plain = experiment.load(tmp_path / "a" / "experiments" / "main.toml")
    assert set(plain.brokers) == {"sim", "shadow", "live"} and not plain.brokers["live"].capped  # type: ignore[union-attr]
    create(tmp_path / "b", Answers(max_qty=5, max_notional=4), vendor=False)
    capped = experiment.load(tmp_path / "b" / "experiments" / "main.toml")
    assert experiment.parked_live_brokers(capped) == ["live"]  # ready, with everything on paper
    assert {capped.broker_of(i) for i in capped.instances} == {"sim"}


def test_init_refuses_to_overwrite_and_checks_names(tmp_path: Path) -> None:
    create(tmp_path, Answers(), vendor=False)
    with pytest.raises(ValueError, match="already has"):
        create(tmp_path, Answers(), vendor=False)
    create(tmp_path, Answers(), force=True, vendor=False)
    with pytest.raises(ValueError, match="module name"):
        create(tmp_path / "x", Answers(strategy="My Strategy"), vendor=False)


def test_init_command_without_questions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTY_CONFIG", "/nonexistent")
    res = runner.invoke(
        cli.app, ["init", str(tmp_path / "q"), "--yes", "--strategy", "s1", "--venue", "polymarket_us"]
    )
    assert res.exit_code == 0, res.output
    assert (tmp_path / "q" / "strategies" / "s1.py").exists()
    assert "[feeds.polymarket_us]" in (tmp_path / "q" / "feeds.toml").read_text()
    bad = runner.invoke(cli.app, ["init", str(tmp_path / "r"), "--yes", "--venue", "nasdaq"])
    assert bad.exit_code == 2 and "unknown venue" in bad.stderr


def test_kalshi_key_travels_inline_in_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from quanty.venues.auth import KalshiSigner, keys

    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    (tmp_path / "k.pem").write_bytes(pem)
    proj = tmp_path / "p"
    create(proj, Answers(kalshi_key_id="kid", kalshi_key_path=str(tmp_path / "k.pem")), vendor=False)
    env = (proj / ".env").read_text()
    assert "KALSHI_PRIVATE_KEY_PATH" not in env and 'KALSHI_SECRET_KEY="-----BEGIN' in env
    for var in ("KALSHI_KEY_ID", "KALSHI_SECRET_KEY", "KALSHI_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(proj)
    keys.cache_clear()
    try:
        assert KalshiSigner.from_env().key_id == "kid"  # the .env value loads as the key
    finally:
        keys.cache_clear()
    with pytest.raises(ValueError, match="key file"):
        create(tmp_path / "q", Answers(kalshi_key_path=str(tmp_path / "missing.pem")), vendor=False)


def test_deploy_files(tmp_path: Path) -> None:
    import tomllib

    create(tmp_path / "My Bot", Answers(), vendor=False)
    fly = tomllib.loads((tmp_path / "My Bot" / "fly.toml").read_text())
    assert fly["app"] == "my-bot" and fly["mounts"]["destination"] == "/app/data"
    assert (
        fly["processes"]["app"].startswith("paper experiments/main.toml")
        and "--ui" in fly["processes"]["app"]
    )
    cloud = (tmp_path / "My Bot" / "deploy" / "cloud-init.yaml").read_text()
    assert cloud.startswith("#cloud-config\n") and "get.docker.com" in cloud and "ufw --force enable" in cloud


def test_project_guides_for_assistants(tmp_path: Path) -> None:
    create(tmp_path / "p", Answers(strategy="edge_finder", venues=["polymarket_us"]), vendor=False)
    guide = (tmp_path / "p" / "AGENTS.md").read_text()
    assert "strategies/edge_finder.py" in guide and 'series = ("polymarket_us:btc_updown_15m",)' in guide
    assert "Never run `quanty live`" in guide and "Never read, print or edit `.env`" in guide
    assert (tmp_path / "p" / "CLAUDE.md").read_text() == "@AGENTS.md\n"  # Claude Code reads the same


def test_polymarket_projects_say_where_the_index_comes_from(tmp_path: Path) -> None:
    create(tmp_path / "pm", Answers(venues=["polymarket_us"]), vendor=False)
    assert '# index_ids = ["BRTI"]' in (tmp_path / "pm" / "feeds.toml").read_text()
    create(tmp_path / "both", Answers(venues=["kalshi", "polymarket_us"]), vendor=False)
    assert "# index_ids" not in (tmp_path / "both" / "feeds.toml").read_text()  # Kalshi's feed has it


def test_upgrade_refreshes_marked_files_and_keeps_your_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from quanty.init import MARK, upgrade

    dest = tmp_path / "bot"
    create(dest, Answers(), vendor=True)  # vendored: upgrade builds from this checkout
    real = subprocess.run

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] in (["uv", "lock"], ["uv", "sync"]):  # no network here
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, **kw)  # type: ignore[call-overload, no-any-return]

    monkeypatch.setattr(subprocess, "run", run)
    readme, compose, strategy = (
        dest / "README.md",
        dest / "compose.yaml",
        dest / "strategies" / "my_strategy.py",
    )
    assert MARK in readme.read_text().splitlines()[0]
    assert (dest / "deploy" / "cloud-init.yaml").read_text().startswith("#cloud-config\n")  # still first
    readme.write_text(readme.read_text() + "\nstale\n")  # an older template's text
    compose.write_text("services: {}  # my own compose, its generated line deleted\n")
    strategy.write_text(strategy.read_text() + "\n# my edge\n")
    (dest / ".dockerignore").write_text(".env\ndata/\n.git/\n__pycache__/\n.venv/\n")  # from before marks
    root = Path(__file__).parents[1]
    what, refreshed, kept = upgrade(dest, root)
    assert what.startswith("quanty ") and len(list((dest / "vendor").glob("quanty_py-*.whl"))) == 1
    assert not (dest / "vendor" / ".gitignore").exists()
    assert "README.md" in refreshed and "stale" not in readme.read_text()
    assert ".dockerignore" in refreshed and MARK in (dest / ".dockerignore").read_text()  # adopted
    assert kept == ["compose.yaml"] and "my own compose" in compose.read_text()
    assert strategy.read_text().endswith("# my edge\n")  # strategies are never touched
    with pytest.raises(ValueError, match="isn't a quanty checkout"):
        upgrade(dest, tmp_path)


def test_upgrade_moves_a_project_from_before_the_rename(tmp_path: Path) -> None:
    import tomllib

    from quanty.init import _point_at

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "bot"\ndependencies = ["quanty", "numpy>=2"]  # mine\n\n'
        "[tool.uv]\npackage = false\n\n[tool.uv.sources]\n"
        'quanty = { path = "vendor/quanty-0.0.1-py3-none-any.whl" }\n'
    )
    _point_at(tmp_path, "vendor/quanty_py-0.1.0-py3-none-any.whl")
    text = (tmp_path / "pyproject.toml").read_text()
    got = tomllib.loads(text)
    assert got["project"]["dependencies"] == ["numpy>=2", "quanty-py"]
    assert got["tool"]["uv"]["sources"] == {"quanty-py": {"path": "vendor/quanty_py-0.1.0-py3-none-any.whl"}}
    assert "# mine" in text and got["tool"]["uv"]["package"] is False  # the rest kept


def test_a_new_project_depends_on_this_release_line_on_pypi(tmp_path: Path) -> None:
    import tomllib

    import quanty

    create(tmp_path / "bot", Answers(), vendor=False)
    got = tomllib.loads((tmp_path / "bot" / "pyproject.toml").read_text())
    major, minor = quanty.__version__.split(".")[:2]
    assert got["project"]["dependencies"] == [f"quanty-py>={quanty.__version__},<{major}.{int(minor) + 1}"]
    assert "sources" not in got["tool"]["uv"]  # nothing vendored, nothing from GitHub
    assert "pip install --system -r pyproject.toml" in (tmp_path / "bot" / "Dockerfile").read_text()


def test_upgrade_on_pypi_takes_the_latest_release_and_its_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from quanty.init import upgrade

    dest = tmp_path / "bot"
    create(dest, Answers(), vendor=False)
    ran: list[list[str]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        out = "refreshed: README.md\nkept: \n" if "--refresh-only" in cmd else "0.2.0\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(subprocess, "run", run)
    what, refreshed, kept = upgrade(dest, None)  # no checkout needed
    assert ["uv", "lock", "-q", "--upgrade-package", "quanty-py"] in ran and ["uv", "sync", "-q"] in ran
    assert ["uv", "run", "quanty", "upgrade", "--refresh-only"] in ran  # the new release's templates
    assert what == "quanty 0.2.0 from PyPI" and refreshed == ["README.md"] and kept == []
