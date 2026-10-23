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
        "pyproject.toml" in written and 'dependencies = ["quanty"]' in (proj / "pyproject.toml").read_text()
    )
    assert ".env" in (proj / ".dockerignore").read_text()  # never baked into the image
    found = plugins.discover([proj / "strategies"], include_examples=False, local=None)
    exp = experiment.load(proj / "experiments" / "main.toml")
    (inst,) = experiment.build(exp, found)
    assert inst.id == "edge_finder" and inst.strategy.series == ("kalshi:KXBTC15M",)
    feeds = (proj / "feeds.toml").read_text()
    assert "[feeds.kalshi]" in feeds and 'slug_prefix = "cpc-btc-updown-15m"' in feeds


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
