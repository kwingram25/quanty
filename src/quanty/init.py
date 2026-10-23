"""`quanty init`: a self-contained project folder with everything you edit and everything to deploy it.

my-quanty/
  .env                     API keys (never committed; owner-only permissions)
  .gitignore, .dockerignore
  feeds.toml               venues and series to connect to
  experiments/main.toml    the strategy, its broker (paper to start) and limits
  strategies/<name>.py     a strategy skeleton aimed at the first series
  Dockerfile, compose.yaml trader + dashboard, persistent data, restart on failure
  deploy/cloud-init.yaml   server setup to paste as "user data" (Docker, firewall, swap, updates)
  fly.toml                 the same project on Fly.io (one machine, a volume for data/)
  vendor/                  a quanty wheel, when init runs from a source checkout
  pyproject.toml           so `uv run quanty ...` works here with nothing installed first
  README.md                next steps
  AGENTS.md, CLAUDE.md     how to work here, for AI coding assistants (Claude Code reads CLAUDE.md)
"""

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import quanty

REPO = "https://github.com/kwingram25/quanty"
TEMPLATE = Path(__file__).resolve().parents[2] / "strategies" / "_template.py"  # in a source checkout

DEFAULT_SERIES = {"kalshi": ["KXBTC15M"], "polymarket_us": ["btc_updown_15m"]}
PM_SERIES = {
    "btc_updown_15m": ("cpc-btc-updown-15m", 900),
    "btc_updown_1h": ("cpc-btc-updown-1h", 3600),
}


@dataclass
class Answers:
    strategy: str = "my_strategy"
    venues: list[str] = field(default_factory=lambda: ["kalshi"])
    series: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_SERIES.items()}
    )
    kalshi_key_id: str = ""
    kalshi_key_path: str = ""
    polymarket_key_id: str = ""
    polymarket_secret: str = ""


def check_name(name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(
            f"strategy name {name!r}: use lowercase letters, digits and _ (a Python module name)"
        )
    return name


def files(a: Answers, name: str = "my-quanty") -> dict[str, str]:
    """Project files, relative path -> content. name: the project's (folder) name."""
    first = next((f"{v}:{a.series[v][0]}" for v in a.venues if a.series.get(v)), "kalshi:KXBTC15M")
    return {
        ".env": _env(a),
        ".gitignore": ".env\ndata/\n__pycache__/\n.venv/\n",
        ".dockerignore": ".env\ndata/\n.git/\n__pycache__/\n.venv/\n",
        "feeds.toml": _feeds(a),
        "experiments/main.toml": _experiment(a),
        f"strategies/{a.strategy}.py": _strategy(a.strategy, first),
        "Dockerfile": DOCKERFILE,
        "compose.yaml": COMPOSE,
        "deploy/cloud-init.yaml": CLOUD_INIT,
        "fly.toml": FLY.format(app=name),
        "README.md": _readme(a, name),
        "AGENTS.md": _agents(a, first),
        "CLAUDE.md": "@AGENTS.md\n",
    }


def create(dest: Path, a: Answers, *, force: bool = False, vendor: bool | None = None) -> list[str]:
    """Write the project; returns what was written. Refuses to overwrite without force.
    vendor: build a quanty wheel into vendor/ (None = when running from a source checkout)."""
    check_name(a.strategy)
    out = files(a, _slug(dest))
    clash = [p for p in out if (dest / p).exists()]
    if clash and not force:
        raise ValueError(f"{dest} already has {', '.join(clash)} (use --force to overwrite)")
    for rel, text in out.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    os.chmod(dest / ".env", 0o600)
    (dest / "data").mkdir(exist_ok=True)
    written = list(out)
    checkout = Path(quanty.__file__).resolve().parents[2]
    if vendor is None:
        vendor = (checkout / "pyproject.toml").exists()
    wheels = _vendor(checkout, dest / "vendor") if vendor else []
    (dest / "pyproject.toml").write_text(_pyproject(dest, wheels[0] if wheels else None))
    return [*written, *wheels, "pyproject.toml"]


def _vendor(checkout: Path, into: Path) -> list[str]:
    into.mkdir(exist_ok=True)
    for old in into.glob("quanty-*.whl"):
        old.unlink()
    try:
        subprocess.run(
            ["uv", "build", "--wheel", "-q", "-o", str(into), str(checkout)],
            check=True,
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [f"vendor/{w.name}" for w in into.glob("quanty-*.whl")]


def _slug(dest: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", dest.resolve().name.lower()).strip("-") or "quanty-project"


def _pyproject(dest: Path, wheel: str | None) -> str:
    """A uv project depending on quanty (the vendored wheel, else GitHub), so `uv run quanty` just works."""
    name = _slug(dest)
    source = f'{{ path = "{wheel}" }}' if wheel else f'{{ git = "{REPO}" }}'
    return f"""[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["quanty"]

[tool.uv]
package = false   # your strategies are loaded from strategies/, not installed

[tool.uv.sources]
quanty = {source}
"""


def _env(a: Answers) -> str:
    lines = [
        "# API keys for the venues this project trades. Never commit this file.",
        "",
        "# Dashboard login (off when unset): `uv run quanty password --write` sets it",
        "# QUANTY_UI_PASSWORD=",
        "",
    ]
    if "kalshi" in a.venues:
        lines += [
            "# Kalshi: key id and PEM private key (Ed25519 or RSA), inline so it travels with .env to a",
            "# server or container (a key file path would point at nothing there)",
            f"KALSHI_KEY_ID={a.kalshi_key_id}",
            f"KALSHI_SECRET_KEY={inline_pem(a.kalshi_key_path) if a.kalshi_key_path else ''}",
            "",
        ]
    if "polymarket_us" in a.venues:
        lines += [
            "# Polymarket US: key id and base64 Ed25519 private key",
            f"POLYMARKET_KEY_ID={a.polymarket_key_id}",
            f"POLYMARKET_SECRET_KEY={a.polymarket_secret}",
            "",
        ]
    return "\n".join(lines)


def inline_pem(path: str) -> str:
    """A PEM key file as one double-quoted .env value, newlines escaped (dotenv, compose and
    `fly secrets import` all read it; quanty also accepts the quotes and escapes left in)."""
    try:
        text = Path(path).expanduser().read_text().strip()
    except OSError as exc:
        raise ValueError(f"Kalshi private key file {path}: {exc.strerror or exc}") from None
    if "-----BEGIN" not in text:
        raise ValueError(f"Kalshi private key file {path}: not a PEM key")
    return '"' + text.replace("\n", "\\n") + '\\n"'


def _feeds(a: Answers) -> str:
    out = ["# Venues and series this project connects to. `quanty feeds list` shows every feed kind.", ""]
    if "kalshi" in a.venues:
        series = ", ".join(f'"{s}"' for s in a.series.get("kalshi") or DEFAULT_SERIES["kalshi"])
        out += [
            "[feeds.kalshi]",
            f"series = [{series}]   # any Kalshi series ticker, e.g. KXHIGHNY (NYC high)",
            'index_ids = ["BRTI"]   # CF Benchmarks indexes BTC markets settle on',
            "",
        ]
    if "polymarket_us" in a.venues:
        out += ["[feeds.polymarket_us]", "series = ["]
        for name in a.series.get("polymarket_us") or DEFAULT_SERIES["polymarket_us"]:
            prefix, period = PM_SERIES.get(name, (name.replace("_", "-"), 900))
            out.append(f'  {{ name = "{name}", slug_prefix = "{prefix}", period_s = {period} }},')
        out += ["]", ""]
        if "kalshi" not in a.venues:
            out += [
                "# These markets settle on CF Benchmarks' BRTI index (cfbenchmarks:BRTI), which only",
                "# Kalshi's feed streams. For strategies that compare the index with the strike, add",
                "# Kalshi keys to .env and uncomment:",
                "# [feeds.kalshi]",
                '# series = ["KXBTC15M"]',
                '# index_ids = ["BRTI"]',
                "",
            ]
    if not a.venues:
        out += ["[feeds.coinbase]   # public data, no keys", 'products = ["BTC-USD"]', ""]
    return "\n".join(out)


def _experiment(a: Answers) -> str:
    return f"""# What runs: `uv run quanty paper experiments/main.toml --feeds feeds.toml`.
# Brokers: kind = "sim" simulates fills (paper); "shadow" also checks every order with the venue
# without placing it; "live" places real orders (needs max_qty, max_notional and `uv run quanty live --arm`).

[limits]               # account-wide
max_open_risk = 100
max_daily_loss = 50

[brokers.main]
kind = "sim"
latency_ms = 100

[[instances]]
id = "{a.strategy}"
strategy = "{a.strategy}"
broker = "main"
[instances.params]     # override the strategy's defaults here (`quanty params {a.strategy}` lists them)
[instances.limits]
max_order_qty = 10
"""


def _strategy(name: str, series: str) -> str:
    text = TEMPLATE.read_text() if TEMPLATE.exists() else FALLBACK_TEMPLATE
    cls = "".join(part.capitalize() for part in name.split("_")) or "MyStrategy"
    text = text.replace(
        '"""Strategy template: copy to strategies/<name>.py and fill in the TODOs.',
        f'"""{name}: TODO one line on what this strategy does.',
    )
    text = text.replace('("TODO_venue:TODO_series",)', f'("{series}",)')
    text = text.replace("class MyStrategy(", f"class {cls}(").replace(
        "class MyParams(", f"class {cls}Params("
    )
    return text.replace("Strategy[MyParams]", f"Strategy[{cls}Params]").replace(
        "= MyParams", f"= {cls}Params"
    )


def _agents(a: Answers, series: str) -> str:
    return f"""# Working in this project

A quanty project: trading strategies for repeated binary prediction markets (Kalshi, Polymarket US),
run the same way in backtest, paper (simulated fills), shadow (orders checked by the venue, not
placed) and live. Run everything through `uv run quanty …`; the first run sets up `.venv/`.

## Files

- `strategies/{a.strategy}.py`: the strategy (one `Strategy` subclass per file). New strategies go in
  `strategies/<name>.py`; the file name is the strategy's name. `quanty list` can also show
  strategies from folders in the user's `~/.config/quanty/config.toml`; leave those alone and don't
  reuse their names.
- `experiments/main.toml`: which strategies run (`[[instances]]`), their params, their broker
  (`kind = "sim"`, `"shadow"` or `"live"`) and risk limits.
- `feeds.toml`: the venues and series to connect to. `.env`: API keys.

## Rules

- **Never run `quanty live`, never set `kind = "live"` or `QUANTY_MODE=live`, and never raise
  `max_qty`, `max_notional` or `[limits]`, unless the user explicitly asks.** These place or permit
  real orders with real money.
- **Never read, print or edit `.env`** (API keys). `uv run quanty doctor` checks it without showing it.
- A strategy only reads events and acts through `ctx`: no network calls, files, clocks
  (`time.time()`) or randomness of its own, so backtests replay exactly what live would do. State on
  `self` is fine (each run builds a fresh instance; nothing on `self` survives a restart).
- Read `self.params` when deciding, not once at startup: params can be edited while a session runs.

## Writing a strategy

```python
from pydantic import Field

from quanty.core import Book, Context, Market, Params, Side, Strategy, Tif


class EdgeParams(Params):
    min_edge: float = Field(0.02, ge=0, description="Buy when the model beats the price by this much")
    size: int = Field(10, gt=0, description="Contracts per order")


class Edge(Strategy[EdgeParams]):
    params_type = EdgeParams
    series = ("{series}",)  # "<venue>:<series>"; empty = every market
    description = "One line on what it does"

    def __init__(self, params: EdgeParams) -> None:
        super().__init__(params)
        self.tried: set[str] = set()  # markets already bought in, filled or not

    def on_book(self, ctx: Context, book: Book) -> None:
        mid = book.market_id
        if mid in self.tried:
            return
        fair = 0.5  # P(YES): the model goes here
        for side, prob in ((Side.YES, fair), (Side.NO, 1 - fair)):
            ask = book.ask(side)
            if ask is not None and prob - ask >= self.params.min_edge:
                self.tried.add(mid)  # once per market: an IOC that misses leaves no order behind
                ctx.buy(mid, side, ask, self.params.size, tif=Tif.IOC)
                return

    def on_market_close(self, ctx: Context, market: Market) -> None:
        self.tried.discard(market.id)
```

**Prices** are in the terms of the side named, in dollars per contract that pays $1: buying NO at
0.97 pays 97¢ for $1 if NO wins. `book.ask(side)` is the price to buy that side now (YES: the best
ask; NO: 1 − the best YES bid) and `book.bid(side)` the price to sell it; both are top of book only,
and None when that side is empty. `book.asks` / `book.bids` hold the levels as
`(yes_price, size)`.

**Markets**: `ctx.market(id)` has `.series`, `.close_ts` and `.terms`, and `.describe()` gives
`title`, `subtitle`, `event`, `strike` (the threshold, if any: Kalshi's `floor_strike`, Polymarket
US's price to beat), `reference` (the data key it settles against) and `unit`. For an up/down
market: `m = ctx.market(mid).describe()`, then `ctx.latest(m["reference"])` is the index's latest
value event (`.value`; None until one arrives), to compare with `m["strike"]`.

**Index data** only arrives if a feed streams it. BTC up/down markets on both venues settle on
`cfbenchmarks:BRTI`, which only the Kalshi feed streams (`index_ids = ["BRTI"]` under
`[feeds.kalshi]` in `feeds.toml`, with Kalshi keys); synthetic markets settle on `synthetic:index`,
always present. A strategy that waits for the index does nothing without it, so log once when it's
missing; `quanty doctor` warns when no feed provides it.

**Hooks** (override what you need): `on_start(ctx)`, `on_market_open(ctx, market)`,
`on_book(ctx, book)`, `on_trade(ctx, trade)`, `on_data(ctx, event)` (index values and other data),
`on_clock(ctx, clock)`, `on_fill(ctx, fill)`, `on_order(ctx, order)`, `on_market_close(ctx, market)`,
then later `on_settled(ctx, market, outcome)`, and `on_params(ctx, old)` after a params edit.

**`ctx`**: `now` (ms), `markets()`, `market(id)`, `seconds_left(id)`, `book(id)`,
`latest(key)` (the latest data event, `.value`, e.g. for `"cfbenchmarks:BRTI"`), `signal(name)`,
`position(id, side)` (contracts held), `entry_price(id, side)` (average cost per contract held,
fees included; None when flat), `orders(id)` (*open* orders only: filled, canceled and
rejected ones are gone), `outcomes(id)`, `buy(id, side, price, qty, tif=, post_only=, tag=)`,
`sell(...)` (only what you hold), `cancel(order)`, `cancel_all(id)`, `log(msg, **fields)` (plain
values: numbers, strings, lists, dicts). `buy`/`sell` return the `Order`: an order that breaks a rule
(price outside 0–1, tick size, a risk limit, market closed) comes back with `.status` REJECTED and a
`.reason` instead of raising. `Tif.IOC` takes what is there and cancels the rest; the default `GTC`
rests until filled, canceled or the market closes.

**Exits and stops**: there is no stop order; a strategy exits by selling what it holds. A model
stop: when the model's probability for the side held drops below a threshold, remember the market
(so the entry logic doesn't buy back in) and `ctx.sell(mid, side, 0.01, ctx.position(mid, side),
tif=Tif.IOC, tag="stop")`, which sells into whatever bids there are. A price stop: compare
`book.bid(side)` with `ctx.entry_price(mid, side)`. IOC sells can fill partly or not at all in a
thin book, and prices jump near the close. Selling works while a strategy is halted. Keep
`tag="stop"` on stop sells: the dashboard shows those as stop losses.
`example_favorite.py` has a model stop (`stop_conf`).

Full reference: {REPO}/blob/main/docs/strategies.md (may be private). Working examples, in the
installed package: `.venv/lib/python3.*/site-packages/quanty/strategies/example_favorite.py` (a
probability model from an index) and `example_bucket.py` (range markets).

## Checking your work

1. `uv run quanty list` shows the strategy (an import error shows here), and
   `uv run quanty params <name>` its params.
2. `uv run quanty demo --strategy <name>` runs it on built-in synthetic markets: no keys, a few
   seconds. A strategy written for a venue's series is pointed at the synthetic 5-minute up/down
   series (`synthetic:updown_5m`, settling on the index `synthetic:index`) automatically; add
   `--param size=2` to override params, `--seed N` for another day, `--hours N` for longer.
3. To check *what* it did, not just the totals: add `--ledger data/backtest/<name>.sqlite` (a new
   file each run: it refuses to overwrite), then either `uv run quanty ui --data data` (the
   dashboard: every trade with its outcome) or query the SQLite tables `orders`, `fills`,
   `settlements`, `markets` and `logs` (times in ms: `orders.created_ts`, `fills.ts`,
   `markets.close_ts`; `side` is `yes`/`no`; `fills.realized` and `settlements.pnl` are P&L after
   fees; `logs.fields` is the JSON from `ctx.log`).
4. Add it to `experiments/main.toml` (an `[[instances]]` entry) to run it with the others. Without
   `--data`, `uv run quanty backtest experiments/main.toml` uses synthetic markets, where only
   instances trading synthetic series trade (it says which can't). Don't put a synthetic `series`
   override in `main.toml`, which paper and live use too; for synthetic runs use `quanty demo`, or
   a separate `experiments/synthetic.toml` whose `[[instances]]` set `series = ["synthetic:updown_5m"]`.
5. On real markets: `uv run quanty record --config feeds.toml --data data --duration 3600` (needs
   each venue's keys in `.env`; without them it stops with an error), then
   `uv run quanty backtest experiments/main.toml --data data`.
6. `uv run quanty paper experiments/main.toml --feeds feeds.toml --data data --ui` paper trades live
   markets with the dashboard on http://127.0.0.1:8765. It runs until stopped: ask before starting
   it, and stop it when done.

A backtest prints, per instance: orders, rejected, fills, contracts, fees, markets won and lost,
and realized P&L in dollars. Zero orders usually means the series doesn't match (it says so) or the
entry conditions never held. Synthetic markets test logic, not edge: a profit there says nothing
about a real venue.
"""


def _readme(a: Answers, name: str) -> str:
    return f"""# {a.strategy}

A quanty project: your strategy, its configuration, and what's needed to deploy it.

## Next steps

1. **Write the strategy** in `strategies/{a.strategy}.py` (the TODOs). `uv run quanty list` should show it
   (the first `uv run` sets up everything it needs in `.venv/`; nothing to install beforehand).
2. **Check your keys** in `.env` with `uv run quanty doctor` (config, keys, clock, each venue; read-only),
   then record some data and look at it:
   `uv run quanty record --config feeds.toml --data data` (Ctrl-C to stop),
   then `uv run quanty ui --data data`.
3. **Backtest** on what you recorded: `uv run quanty backtest experiments/main.toml --data data`.
4. **Paper trade** live markets with simulated fills, with the dashboard alongside:
   `uv run quanty paper experiments/main.toml --feeds feeds.toml --data data --ui`.
5. **Shadow, then live**: set `kind = "shadow"` in `experiments/main.toml` to have every order checked
   by the venue; for real orders, `kind = "live"` with `max_qty` and `max_notional`, and
   `uv run quanty live experiments/main.toml --feeds feeds.toml --data data --arm`.

## Deploy

Any Linux server with Docker (a DigitalOcean Droplet, AWS EC2 or Lightsail, Hetzner, ...): create it
with `deploy/cloud-init.yaml` pasted as its "user data", then from this folder:

    uv run quanty deploy root@your-server

That copies this folder (with `.env`; never `data/`), checks it with `quanty doctor` on the server,
and starts the trader (paper by default; `QUANTY_MODE=live` in `.env` trades for real) and the
dashboard, both restarted on failure and on reboot. Run it again after every change. The dashboard
listens on the server's 127.0.0.1:8765: `ssh -N -L 8765:127.0.0.1:8765 root@your-server`, then open
http://127.0.0.1:8765.

On Fly.io, `fly.toml` runs the same project on one machine with a volume for `data/`. Managed container
services that wipe the disk on redeploy (App Platform, ECS/Fargate, Cloud Run, Render) don't fit:
quanty keeps its state in `data/`. The full guide, per provider: {REPO}/blob/main/docs/deploying.md

quanty is installed in the image from `vendor/*.whl` if there is one, otherwise from {REPO}
(which needs access if the repository is private). Refresh the wheel from a quanty checkout with
`uv build --wheel -o vendor <path to quanty>`.
"""


DOCKERFILE = """FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app
# quanty: a wheel in vendor/ when there is one, else from GitHub
ARG QUANTY_SOURCE="quanty @ git+https://github.com/kwingram25/quanty"
COPY . .
RUN if ls vendor/quanty-*.whl >/dev/null 2>&1; then uv pip install --system vendor/quanty-*.whl; \\
    else apt-get update && apt-get install -y --no-install-recommends git \\
    && uv pip install --system "$QUANTY_SOURCE"; fi
ENTRYPOINT ["quanty"]
"""

COMPOSE = """# docker compose up -d --build: the trader and the dashboard, sharing data/, restarted on failure
services:
  trader:
    build: .
    env_file: .env
    volumes: [./data:/app/data]
    # paper (simulated fills) unless QUANTY_MODE=live in .env; live also needs --arm, added here
    entrypoint: ["sh", "-c"]
    command:
      - >-
        if [ "$${QUANTY_MODE:-paper}" = live ];
        then exec quanty live experiments/main.toml --feeds feeds.toml --data data --arm;
        else exec quanty paper experiments/main.toml --feeds feeds.toml --data data; fi
    restart: unless-stopped

  dashboard:
    build: .
    env_file: .env
    volumes: [./data:/app/data]
    command: ["ui", "--data", "data", "--host", "0.0.0.0", "--port", "8765"]
    ports: ["127.0.0.1:8765:8765"]   # the server's localhost only: reach it with an SSH tunnel, or...
    restart: unless-stopped

  # ...from anywhere over HTTPS: set a password (`uv run quanty password --write`), point a domain at
  # this server, put it below, and uncomment. Caddy gets and renews the certificate itself.
  # https:
  #   image: caddy:2
  #   command: caddy reverse-proxy --from dashboard.example.com --to dashboard:8765
  #   ports: ["80:80", "443:443"]
  #   volumes: [caddy:/data]
  #   restart: unless-stopped

# volumes:
#   caddy: {}
"""

CLOUD_INIT = """#cloud-config
# Server setup: paste as "user data" when creating it (DigitalOcean: Advanced options > Add
# initialization scripts; AWS EC2: Advanced details > User data). For Ubuntu or Debian images.
# It installs Docker and lets the login user run it, turns on the firewall with only SSH open, adds
# 2 GB of swap (the image build needs it on a 1 GB server), and keeps security updates automatic.
# Ports Docker publishes skip the firewall: compose.yaml publishes the dashboard on 127.0.0.1 only
# (and 80/443 if you enable its https service). Then, from the project: uv run quanty deploy you@server
package_update: true
packages: [ca-certificates, curl, rsync, ufw, unattended-upgrades]
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - for u in ubuntu admin debian; do id "$u" >/dev/null 2>&1 && usermod -aG docker "$u"; done; true
  - ufw allow 22/tcp
  - ufw --force enable
  - fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  - echo '/swapfile none swap sw 0 0' >> /etc/fstab
"""

FLY = """# Fly.io: one machine runs the trader and the dashboard, with data/ on a volume. First time:
#   fly apps create {app}                         (the name must be unique on Fly; change it here too)
#   fly volumes create data --size 10 --region iad
#   grep -E '^[A-Z_]+=.' .env | fly secrets import
#   fly deploy --ha=false
# After changes: fly deploy. Dashboard: fly proxy 8765:8765, then open http://127.0.0.1:8765
# (it listens on Fly's private network only, reachable by your organization).
app = "{app}"
primary_region = "iad"

[build]
  dockerfile = "Dockerfile"

[processes]
  # arguments to the image's `quanty`; for real orders: "live experiments/main.toml ... --arm --ui ..."
  app = "paper experiments/main.toml --feeds feeds.toml --data data --ui --ui-host ::"

[mounts]
  source = "data"
  destination = "/app/data"

[[vm]]
  memory = "1gb"
  cpu_kind = "shared"
  cpus = 1
"""

FALLBACK_TEMPLATE = '''"""Strategy template: copy to strategies/<name>.py and fill in the TODOs."""

from pydantic import Field

from quanty.core import Book, Context, Params, Side, Strategy, Tif


class MyParams(Params):
    min_edge: float = Field(0.02, ge=0, description="Buy when the model beats the price by this much")
    size: int = Field(1, gt=0, description="Contracts per order")


class MyStrategy(Strategy[MyParams]):
    params_type = MyParams
    series = ("TODO_venue:TODO_series",)
    description = "TODO: one line on what this strategy does"

    def fair_yes(self, ctx: Context, market_id: str) -> float | None:
        """TODO: your model. Probability that YES wins, or None to skip."""
        return None

    def on_book(self, ctx: Context, book: Book) -> None:
        fair = self.fair_yes(ctx, book.market_id)
        if fair is None or ctx.orders(book.market_id):
            return
        ask = book.ask(Side.YES)
        if ask is not None and fair - ask >= self.params.min_edge:
            ctx.buy(book.market_id, Side.YES, ask, self.params.size, tif=Tif.IOC)
'''
