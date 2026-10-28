import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from quanty import cli
from quanty.deploy import commands, handover
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


FAKE_DOCKER = """#!/bin/sh
echo "$*" >> calls
case "$*" in
  "compose ps -q recorder") if [ -f recreated ]; then printf 'tmp-id\\nnew\\n'; else echo old; fi ;;
  "compose run -d --rm --no-deps recorder") echo tmp ;;
  "inspect -f {{.Id}} tmp") echo tmp-id ;;
  "compose up -d --no-deps recorder") touch recreated ;;
  "logs tmp"|"logs new") if [ -f starts ]; then echo "recording beside it"; fi ;;
esac
"""


@pytest.mark.parametrize("starts", [True, False])
def test_deploy_hands_the_recorder_over_without_a_gap(tmp_path: Path, starts: bool) -> None:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(FAKE_DOCKER)
    (bin_ / "docker").chmod(0o755)
    (tmp_path / "compose.yaml").write_text('command: ["record", "--take-over"]\n')
    if starts:
        (tmp_path / "starts").touch()
    env = {"PATH": f"{bin_}:/usr/bin:/bin"}
    script = handover(wait_s=2, settle_s=0)
    res = subprocess.run(["sh", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert res.returncode == 0  # never stops a deploy: compose up -d follows either way
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[1] == "compose run -d --rm --no-deps recorder"  # a one-off on the new image, beside
    if starts:  # recreated once the one-off records; the one-off stops once the new one does
        assert calls[-4:] == ["inspect -f {{.Id}} tmp", "compose ps -q recorder", "logs new", "stop tmp"]
        assert "compose up -d --no-deps recorder" in calls and "recorder handed over" in res.stdout
    else:  # the one-off goes; compose up -d then recreates the recorder as before
        assert calls[-1] == "rm -f tmp" and "compose up -d --no-deps recorder" not in calls
    (tmp_path / "compose.yaml").write_text('command: ["record"]\n')  # without --take-over: nothing
    (tmp_path / "calls").unlink()
    subprocess.run(["sh", "-c", script], cwd=tmp_path, env=env, check=True)
    assert (tmp_path / "calls").read_text().splitlines() == ["compose ps -q recorder"]


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
