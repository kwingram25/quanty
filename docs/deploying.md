# Deploying

`quanty init` creates a project folder that runs the same on your laptop and on a server:

```sh
uv run quanty init my-quanty     # strategy, feeds.toml, experiments/main.toml, .env, Docker and deploy files
```

Inside the project, `uv run quanty …` works with nothing installed first: the project's
`pyproject.toml` points at quanty (the wheel in `vendor/`, else GitHub), and the first `uv run` sets
up `.venv/`.

## Where it can run

quanty is a long-running process that keeps its state on disk, in `data/`: SQLite ledgers, the live
order journal (how a restarted live run finds orders a crashed one left behind), and recordings.
So it needs a machine whose disk survives restarts and redeploys.

| Where | Fits? |
|---|---|
| A Linux server with Docker: DigitalOcean Droplet, AWS EC2 or Lightsail, Hetzner, Linode, GCP Compute Engine, a box at home | Yes: the main path, below |
| Fly.io | Yes: one machine with a volume ([below](#flyio)) |
| App Platform, ECS/Fargate, Cloud Run, Render, Railway, Heroku | No. Their disks are wiped on redeploy or restart; the trader and dashboard can't share a folder; their network storage (EFS and the like) isn't safe for SQLite; some stop idle services |

Pick a US region: both venues are US-regulated. If you already run other trading bots, give quanty
its own server: Polymarket US limits requests per IP address (25 a second), so bots on one address
share that budget. (Kalshi limits per account, wherever the bots run; `quanty doctor` shows it.)

## On a server with Docker

### 1. Create the server

Whatever the provider: **Ubuntu 24.04 LTS**, 2 GB of memory (1 GB works, with the swap below),
25 GB of disk or more (recordings grow by gigabytes a day), your SSH key, and the project's
`deploy/cloud-init.yaml` pasted into the provider's **user data** field. That file installs Docker,
turns on the firewall with only SSH open, adds 2 GB of swap and keeps security updates automatic;
it's done a minute or two after the server boots.

**DigitalOcean.** Create → Droplets. A New York or San Francisco region; Ubuntu 24.04; a Basic
Droplet with 2 GB; your SSH key; under *Advanced options*, *Add initialization scripts*, paste
`deploy/cloud-init.yaml`. For data that outlives the Droplet, add a Volume while creating it with
*Automatically format & mount*: it appears at `/mnt/<volume name>`, and you deploy onto it with
`--to /mnt/<volume name>/my-quanty` in step 2. Optionally add a Cloud Firewall allowing only SSH
in. You log in as `root`.

**AWS EC2.** Launch instance in `us-east-1` (or another US region): Ubuntu Server 24.04 LTS,
`t3.small`, your key pair, a security group allowing SSH from your IP only, 30 GB gp3 storage, and
under *Advanced details*, *User data*, paste `deploy/cloud-init.yaml`. Attach an Elastic IP if you
want the address to survive stopping the instance. You log in as `ubuntu`. The disk is deleted when
the instance is *terminated* (not when stopped).

**AWS Lightsail** is the simpler, fixed-price version: Create instance → Linux/Unix → OS Only →
Ubuntu 24.04, *Add launch script* and paste `deploy/cloud-init.yaml`, a 2 GB plan. Its firewall
opens HTTP by default: remove that rule, keep SSH. You log in as `ubuntu`.

**Hetzner, Linode, GCP and others** have the same field, called *user data*, *cloud config* or
*startup script* (on GCP, a `user-data` metadata key). Hetzner's US locations are Ashburn and
Hillsboro.

### 2. Deploy

From the project folder on your machine:

```sh
uv run quanty deploy root@203.0.113.7          # or ubuntu@… on AWS, or a name from ~/.ssh/config
```

It copies the project to the server (with `.env`, kept owner-only; never `data/`), builds the image
there, runs `quanty doctor` inside it, and only if that passes, starts the trader and the dashboard.
Run the same command after every change: the containers restart on the new code, and `data/` on the
server is never sent or deleted. `--to` picks the folder on the server (default: the project's name,
in your home), `--dry-run` prints the commands, and `--no-check` skips the doctor step. It needs
`ssh` and `rsync`, which macOS and Linux have.

What runs, in two containers sharing `data/`, each restarted on failure and on reboot:

- **trader**: `quanty paper experiments/main.toml --feeds feeds.toml`, or `quanty live … --arm` when
  `.env` sets `QUANTY_MODE=live`. Recordings, ledgers and the order journal live in `data/`.
- **dashboard**: `quanty ui`, published on the server's `127.0.0.1:8765` only.

On the server, `docker compose logs -f trader` follows the trader (from the project folder there).
Without `quanty deploy`, the same by hand: copy the folder with `.env`, then
`docker compose up -d --build`.

### 3. Open the dashboard

```sh
ssh -N -L 8765:127.0.0.1:8765 root@203.0.113.7
```

then open http://127.0.0.1:8765. SSH is the login, so no password is needed this way. To reach it
from anywhere instead, see [below](#reaching-the-dashboard-from-anywhere).

### Getting quanty into the image

The Dockerfile installs quanty from a wheel in `vendor/` if there is one (`quanty init` builds it when
run from a quanty source checkout), otherwise from GitHub, which needs access while the repository
is private. To update the wheel after pulling a newer quanty: `uv build --wheel -o vendor
/path/to/quanty`, then deploy again.

### API keys on a server

Your keys travel in `.env`, which `.dockerignore` keeps out of the image; compose passes them to the
containers at run time. Keep the Kalshi key itself in `.env` (`KALSHI_SECRET_KEY`, as `quanty init`
writes it), not a `KALSHI_PRIVATE_KEY_PATH` to a file on your laptop, which wouldn't exist there;
`quanty doctor` flags it.

## Fly.io

`fly.toml` in the project runs everything on one Fly machine: `quanty paper … --ui` starts the
trader with the dashboard beside it (in its own process), and `data/` lives on a Fly volume, which
attaches to one machine (hence `--ha=false`). With [flyctl](https://fly.io/docs/flyctl/install/),
from the project folder:

```sh
fly apps create my-quanty                        # a name unique on Fly; the same as app = in fly.toml
fly volumes create data --size 10 --region iad
grep -E '^[A-Z_]+=.' .env | fly secrets import   # your keys, as Fly secrets
fly deploy --ha=false
```

After changes, `fly deploy` again. The dashboard listens on your organization's private network
only: `fly proxy 8765:8765`, then open http://127.0.0.1:8765. Logs: `fly logs`. For real orders,
change the `[processes]` command in `fly.toml` to `live experiments/main.toml --feeds feeds.toml
--data data --arm --ui --ui-host ::`. To run the doctor there:
`fly ssh console -C "sh -c 'cd /app && quanty doctor'"`.

The generated `fly.toml` is valid TOML (quanty's tests check that) but hasn't been run on a Fly
account yet; if a `fly` command disagrees with it, Fly's error is the authority.

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
commands under a process supervisor (or one: `quanty paper … --ui` serves the dashboard too). With
systemd, one unit each:

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

`quanty deploy` runs `quanty doctor` on the server each time; run it by hand with
`docker compose run --rm --entrypoint quanty trader doctor` in the project folder there. It checks, read-only: the experiments, strategies and `feeds.toml` load; keys are present, valid, and
`.env` is private; the server's clock agrees with each venue's (signed requests carry a timestamp,
so a drifting clock gets orders refused); each venue is reachable, accepts the keys, and has
rate-limit headroom (a 429 now means something else is using this account's budget); no orders
are left unresolved by a previous live run; and there's disk space for recordings. It exits 1 if
anything fails, so a deploy script can run it first. `--offline` skips the network checks.

Read [Live trading](live-trading.md): paper first, then shadow, then one small order watched closely,
with `max_qty` and `max_notional` set to amounts you would not mind getting wrong.
