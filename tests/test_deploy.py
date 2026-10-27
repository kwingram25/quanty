import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from quanty import cli
from quanty.deploy import commands
from quanty.init import Answers, create


def test_commands_never_touch_data(tmp_path: Path) -> None:
    create(tmp_path / "bot", Answers(), vendor=False)
    mkdir, rsync, remote = commands("root@srv", tmp_path / "bot", "/mnt/vol/bot")
    assert mkdir == ["ssh", "root@srv", "mkdir -p /mnt/vol/bot"]
    assert "--exclude=data/" in rsync and "--delete" in rsync and rsync[-1] == "root@srv:/mnt/vol/bot/"
    assert "--exclude=*.tfstate*" in rsync  # OpenTofu's state stays home
    steps = remote[2].split(" && ")
    assert steps[0] == "cd /mnt/vol/bot" and steps.index(
        "docker compose run --rm --entrypoint quanty trader doctor"
    ) < steps.index("docker compose up -d")
    assert "doctor" not in commands("h", tmp_path / "bot", "x", check=False)[2][2]
    with pytest.raises(ValueError, match=r"no compose\.yaml"):
        commands("h", tmp_path, "x")


def test_deploy_dry_run(tmp_path: Path) -> None:
    create(tmp_path / "bot", Answers(), vendor=False)
    res = CliRunner().invoke(cli.app, ["deploy", "me@srv", "--path", str(tmp_path / "bot"), "--dry-run"])
    assert res.exit_code == 0 and "$ ssh me@srv 'mkdir -p bot'" in res.output and "rsync -az" in res.output


def test_python_m_quanty_serves_the_dashboard(tmp_path: Path) -> None:
    # what `paper --ui` / `live --ui` start next to the session
    with cli._dashboard(tmp_path, "127.0.0.1", 18765):
        for _ in range(100):
            try:
                if httpx.get("http://127.0.0.1:18765/api/runs").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("dashboard didn't start")
    with pytest.raises(httpx.HTTPError):
        httpx.get("http://127.0.0.1:18765/api/runs", timeout=1)  # stopped with the session
    assert subprocess.run([sys.executable, "-m", "quanty", "--help"], capture_output=True).returncode == 0
