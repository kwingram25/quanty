"""`quanty init`: a self-contained project folder with everything you edit and everything to deploy it.

my-quanty/
  .env                     API keys (never committed; owner-only permissions)
  .gitignore, .dockerignore
  feeds.toml               venues and series to connect to
  experiments/main.toml    the strategy, its broker (paper to start) and limits
  strategies/<name>.py     a strategy skeleton aimed at the first series
  Dockerfile, compose.yaml trader + dashboard, persistent data, restart on failure
  vendor/                  a quanty wheel, when init runs from a source checkout
  pyproject.toml           so `uv run quanty ...` works here with nothing installed first
  README.md                next steps
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


def files(a: Answers) -> dict[str, str]:
    """Project files, relative path -> content."""
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
        "README.md": _readme(a),
    }


def create(dest: Path, a: Answers, *, force: bool = False, vendor: bool | None = None) -> list[str]:
    """Write the project; returns what was written. Refuses to overwrite without force.
    vendor: build a quanty wheel into vendor/ (None = when running from a source checkout)."""
    check_name(a.strategy)
    out = files(a)
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


def _pyproject(dest: Path, wheel: str | None) -> str:
    """A uv project depending on quanty (the vendored wheel, else GitHub), so `uv run quanty` just works."""
    name = re.sub(r"[^a-z0-9]+", "-", dest.resolve().name.lower()).strip("-") or "quanty-project"
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
            "# Kalshi: key id and PEM private key file (Ed25519 or RSA)",
            f"KALSHI_KEY_ID={a.kalshi_key_id}",
            f"KALSHI_PRIVATE_KEY_PATH={a.kalshi_key_path}",
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


def _readme(a: Answers) -> str:
    return f"""# {a.strategy}

A quanty project: your strategy, its configuration, and what's needed to deploy it.

## Next steps

1. **Write the strategy** in `strategies/{a.strategy}.py` (the TODOs). `uv run quanty list` should show it
   (the first `uv run` sets up everything it needs in `.venv/`; nothing to install beforehand).
2. **Check your keys** in `.env`, then record some data and look at it:
   `uv run quanty record --config feeds.toml --data data` (Ctrl-C to stop),
   then `uv run quanty ui --data data`.
3. **Backtest** on what you recorded: `uv run quanty backtest experiments/main.toml --data data`.
4. **Paper trade** live markets with simulated fills, with the dashboard alongside:
   `uv run quanty paper experiments/main.toml --feeds feeds.toml --data data`
   and `uv run quanty ui --data data`.
5. **Shadow, then live**: set `kind = "shadow"` in `experiments/main.toml` to have every order checked
   by the venue; for real orders, `kind = "live"` with `max_qty` and `max_notional`, and
   `uv run quanty live experiments/main.toml --feeds feeds.toml --data data --arm`.

## Deploy

On a server with Docker, copy this folder (with `.env`) and run:

    docker compose up -d --build

That starts the trader (paper by default; set `QUANTY_MODE=live` in `.env` to trade for real) and the
dashboard on 127.0.0.1:8765 of the server. Reach it through an SSH tunnel:
`ssh -L 8765:127.0.0.1:8765 you@server`, then open http://127.0.0.1:8765. Data persists in `data/`;
both restart on failure and on reboot. After editing a strategy, `docker compose up -d --build` again.

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
