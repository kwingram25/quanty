# Deploying

`quanty init` creates a project folder that runs the same on your laptop and on a server:

```sh
uv run quanty init my-quanty     # strategy skeleton, feeds.toml, experiments/main.toml, .env, Dockerfile, compose.yaml
```

Inside the project, `uv run quanty …` works with nothing installed first: the project's
`pyproject.toml` points at quanty (the wheel in `vendor/`, else GitHub), and the first `uv run` sets
up `.venv/`.

## With Docker (recommended)

Copy the folder, including `.env`, to a server with Docker, then:

```sh
cd my-quanty
docker compose up -d --build
```

This runs two containers sharing `data/`:

- **trader**: `quanty paper experiments/main.toml --feeds feeds.toml`, or `quanty live … --arm` when
  `.env` sets `QUANTY_MODE=live`. Recordings, ledgers and the order journal live in `data/`.
- **dashboard**: `quanty ui`, published on the server's `127.0.0.1:8765` only, because it has no
  login. Reach it with an SSH tunnel: `ssh -L 8765:127.0.0.1:8765 you@server`, then open
  http://127.0.0.1:8765.

Both restart on failure and when the server reboots (`restart: unless-stopped`). After changing a
strategy or config, run `docker compose up -d --build` again. `docker compose logs -f trader` follows
the trader's output.

Your API keys stay in `.env`, which `.dockerignore` keeps out of the image; compose passes them to the
containers at run time.

### Getting quanty into the image

The Dockerfile installs quanty from a wheel in `vendor/` if there is one (`quanty init` builds it when
run from a quanty source checkout), otherwise from GitHub. To update the wheel after pulling a newer
quanty: `uv build --wheel -o vendor /path/to/quanty`, then rebuild.

## Reaching the dashboard from anywhere

The dashboard has an optional login. Set a password (only a salted hash is stored, in `.env`):

```sh
uv run quanty password --write
```

With a password set, every page, API call and live update needs signing in first; sessions last 7
days, and repeated wrong passwords lock the address out for a minute. A password alone is not
enough over plain HTTP, though: it would cross the network in the clear. Put the dashboard behind
HTTPS. In `compose.yaml`, point a domain at the server and uncomment the `https` service and the
`caddy` volume: Caddy fetches and renews a certificate for the domain and forwards to the dashboard,
which stays unpublished beyond the server itself. Then `docker compose up -d`.

`quanty ui` warns if it listens beyond localhost without a password.

## Without Docker

Install quanty on the server (`uv tool install /path/to/quanty`, or from the wheel) and run the two
commands under a process supervisor. With systemd, one unit each:

```ini
# /etc/systemd/system/quanty-trader.service
[Service]
WorkingDirectory=/srv/my-quanty
EnvironmentFile=/srv/my-quanty/.env
ExecStart=/usr/local/bin/quanty paper experiments/main.toml --feeds feeds.toml --data data
Restart=on-failure
User=quanty

[Install]
WantedBy=multi-user.target
```

and the same with `ExecStart=/usr/local/bin/quanty ui --data data` for `quanty-ui.service`. Then
`systemctl enable --now quanty-trader quanty-ui`.

## Before going live

Read [Live trading](live-trading.md): paper first, then shadow, then one small order watched closely,
with `max_qty` and `max_notional` set to amounts you would not mind getting wrong.
