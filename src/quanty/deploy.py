"""`quanty deploy you@server`: copy a project to a Docker server, check it there, and (re)start it.

The same command for the first deploy and every update. It runs, in order:

1. `ssh` to make the project folder on the server
2. `rsync` of the project, never .env, data/, .venv/ or .git/
3. on the server: stop unless the server has its keys (.env, put there by `quanty keys`), record what
   was sent (the git commit, and whether it had uncommitted changes) in .quanty-deployed, build the
   image, `quanty doctor` in it (stops here if anything fails), a recorder started with --take-over
   replaced without a gap (handover), then `docker compose up -d`, which restarts the trader and the
   dashboard on the new code

data/ on the server (recordings, ledgers, the live order journal) and its .env are never sent or
deleted: keys travel apart from code, once, with `quanty keys you@server --file .env.server`.
"""

import json
import shlex
from pathlib import Path
from typing import Any

from quanty.version import DEPLOYED_MARK

EXCLUDE = (
    "data/",
    ".venv/",
    ".git/",
    "__pycache__/",
    ".DS_Store",
    ".env",
    ".env.*",
    ".terraform/",
    "*.tfstate*",
    DEPLOYED_MARK,
)
KEY_NAMES = ("KALSHI_KEY_ID", "KALSHI_SECRET_KEY", "POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY")


def handover(wait_s: int = 120, settle_s: int = 10) -> str:
    """Shell that replaces a recorder started with --take-over without a gap in the recordings. A
    one-off recorder on the new image starts beside the running one (into the hour's handover file),
    the recorder is recreated (the one-off takes over as the old one stops; the new one starts beside
    it), then the one-off stops and the new recorder takes over. Each step waits for the recorder it
    started to record beside the other (then settle_s, for its other feeds). Without --take-over, or
    if the one-off isn't there within wait_s, `docker compose up -d` recreates the recorder, and
    nothing is recorded while the new container starts."""
    return (
        "{ beside() { for i in $(seq "
        + str(wait_s)
        + '); do docker logs "$1" 2>&1 | grep -q "recording beside" && sleep '
        + str(settle_s)
        + " && return 0; sleep 1; done; return 1; };"
        ' if [ -n "$(docker compose ps -q recorder 2>/dev/null)" ]'
        " && grep -q -- --take-over compose.yaml; then"
        " tmp=$(docker compose run -d --rm --no-deps recorder);"
        ' if [ -n "$tmp" ] && beside "$tmp"; then'
        " docker compose up -d --no-deps recorder;"
        " tmp_id=$(docker inspect -f '{{.Id}}' \"$tmp\");"
        ' new=$(docker compose ps -q recorder | grep -vx "$tmp_id" | head -n 1);'
        ' beside "$new" || echo "the new recorder did not start beside the one-off" >&2;'
        ' docker stop "$tmp" >/dev/null; echo "recorder handed over";'
        ' else echo "the new recorder did not start beside the old one: recreating it" >&2;'
        ' docker rm -f "$tmp" >/dev/null 2>&1; fi;'
        " fi; true; }"
    )


def commands(
    host: str, project: Path, remote_dir: str, check: bool = True, stamp: dict[str, Any] | None = None
) -> list[list[str]]:
    """stamp: what's being sent (quanty.version.git), kept on the server for the runs to record."""
    for need in ("compose.yaml", "Dockerfile"):
        if not (project / need).exists():
            raise ValueError(f"{project.resolve()} has no {need}: run this in a folder from `quanty init`")
    d = shlex.quote(remote_dir)
    rsync = ["rsync", "-az", "--delete", *(f"--exclude={x}" for x in EXCLUDE)]
    # the marker: sessions here keep dashboard changes with the run (the next deploy replaces these
    # files); `quanty pull` and `quanty deploy` bring them into the local experiment files. It holds
    # the commit sent, which each run there records (the image has no .git)
    mark = shlex.quote(json.dumps(stamp or {}, separators=(",", ":")))
    no_keys = (
        f"no .env on the server: send its keys first (quanty keys HOST --file .env.server{_to(remote_dir)})"
    )
    steps = [
        f"cd {d}",
        f"{{ test -f .env || {{ echo {shlex.quote(no_keys)} >&2; exit 4; }}; }}",
        f"printf '%s\\n' {mark} > {DEPLOYED_MARK}",
        "docker compose build",
    ]
    if check:
        steps.append("docker compose run --rm --entrypoint quanty trader doctor")
    steps += [handover(), "docker compose up -d", "docker compose ps"]
    return [
        ["ssh", host, f"mkdir -p {d}"],
        [*rsync, f"{project.resolve()}/", f"{host}:{remote_dir.rstrip('/')}/"],
        ["ssh", host, " && ".join(steps)],
    ]


def _to(remote_dir: str) -> str:
    return "" if "/" not in remote_dir and not remote_dir.startswith("~") else f" --to {remote_dir}"


def check_keys(path: Path) -> list[str]:
    """The names a keys file sets (never their values): API keys and settings like QUANTY_UI_PASSWORD;
    refuses a file with none of the API keys."""
    names = []
    for line in path.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.lstrip().startswith("#") and k.strip() and v.strip():
            names.append(k.strip())
            if "$" in v and not (v.strip().startswith("'") and v.strip().endswith("'")):
                raise ValueError(
                    f"{path}: {k.strip()} contains `$`, which Docker Compose would substitute as a variable "
                    "(a QUANTY_UI_PASSWORD hash ends up blank): put the value in single quotes"
                )
    if not set(names) & set(KEY_NAMES):
        raise ValueError(f"{path} sets none of {', '.join(KEY_NAMES)}")
    return names


def keys_command(host: str, remote_dir: str) -> list[str]:
    """ssh that writes stdin as the server's .env (owner-only, replaced atomically), then recreates
    the containers if they're running, since they read .env when created."""
    d = shlex.quote(remote_dir)
    script = (
        f"mkdir -p {d} && cd {d} && umask 077 && cat > .env.new && chmod 600 .env.new && mv .env.new .env"
        ' && if test -f compose.yaml && test -n "$(docker compose ps -q 2>/dev/null)";'
        " then docker compose up -d --force-recreate; fi"
    )
    return ["ssh", host, script]


def server_script(tailscale: bool = False, hostname: str = "") -> str:
    """Bash that prepares a fresh Ubuntu or Debian server for `quanty deploy`, safe to run again:
    Docker (from Docker's signed apt repository), a firewall with only SSH open, swap on a small
    machine, automatic security updates, and with tailscale, Tailscale with the dashboard served
    to your tailnet only. Each step checks first and skips what's already there."""
    tail = ""
    if tailscale:
        name = shlex.quote(hostname) if hostname else '"$(hostname)"'
        tail = f"""
step "Tailscale"
if ! command -v tailscale >/dev/null; then
  ts="https://pkgs.tailscale.com/stable/$ID/$VERSION_CODENAME"
  curl -fsSL "$ts.noarmor.gpg" > /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "$ts.tailscale-keyring.list" > /etc/apt/sources.list.d/tailscale.list
  apt-get -qq update && apt-get -qq install -y tailscale >/dev/null
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "approve this server in your Tailscale account (the link below), then this continues:"
  tailscale up --hostname={name}
fi
if tailscale serve --bg --https=443 http://127.0.0.1:8765 >/dev/null 2>&1; then
  name=$(tailscale status --json | sed -n 's/.*"DNSName": *"\\([^"]*\\)\\.".*/\\1/p' | head -1)
  url="https://$name"
  echo "dashboard: $url (your tailnet only)"
else
  echo "! tailscale serve failed: turn on MagicDNS and HTTPS Certificates"
  echo "  (Tailscale admin console, DNS), then run this again"
fi"""
    return f"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
step() {{ echo "== $*"; }}
. /etc/os-release
case "$ID" in
  ubuntu|debian) ;;
  *) echo "quanty server sets up Ubuntu or Debian; this is $ID" >&2; exit 2;;
esac

step "packages"
apt-get -qq update && apt-get -qq install -y ca-certificates curl rsync ufw unattended-upgrades >/dev/null

step "Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \\
    "https://download.docker.com/linux/$ID $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
  apt-get -qq update && apt-get -qq install -y docker-ce docker-ce-cli containerd.io \\
    docker-buildx-plugin docker-compose-plugin >/dev/null
fi
if [ -n "${{SUDO_USER:-}}" ] && [ "$SUDO_USER" != root ]; then usermod -aG docker "$SUDO_USER"; fi
docker --version

step "firewall: SSH only (the dashboard stays on 127.0.0.1)"
ufw allow 22/tcp >/dev/null && ufw --force enable >/dev/null && ufw status | head -1

step "swap"
mem_kb=$(awk '/MemTotal/ {{print $2}}' /proc/meminfo)
if [ -z "$(swapon --show)" ] && [ "$mem_kb" -lt 4200000 ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show | tail -n +2 | head -1 || true

step "automatic security updates"
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
systemctl is-active unattended-upgrades
{tail}
echo "== ready: from the project, quanty keys, then quanty deploy"
"""
