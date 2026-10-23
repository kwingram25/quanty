"""API keys and request signing. Keys come from the environment or .env and are never logged.

Only live connections need keys; parsing and replaying recordings never do.
"""

import base64
import re
import time
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_KEYS = (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)


class Keys(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    polymarket_key_id: SecretStr | None = None
    polymarket_secret_key: SecretStr | None = None  # base64 Ed25519 private key
    kalshi_key_id: SecretStr | None = None
    kalshi_secret_key: SecretStr | None = None  # PEM (Ed25519 or RSA); may be on one line
    kalshi_private_key_path: Path | None = None  # or a PEM file
    quanty_ui_password: SecretStr | None = None  # dashboard login: a hash from `quanty password`


@lru_cache(maxsize=1)
def keys() -> Keys:
    return Keys()


def _need(value: SecretStr | None, name: str) -> str:
    if value is None or not value.get_secret_value().strip():
        raise RuntimeError(f"{name} is not set (in the environment or .env); see .env.example")
    return value.get_secret_value().strip().strip('"')


def _now_ms() -> str:
    return str(time.time_ns() // 1_000_000)


class PolymarketSigner:
    """Polymarket US: Ed25519 signature over timestamp + method + path."""

    def __init__(self, key_id: str, secret_b64: str) -> None:
        self.key_id = key_id
        self._key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret_b64)[:32])

    @classmethod
    def from_env(cls) -> "PolymarketSigner":
        k = keys()
        return cls(
            _need(k.polymarket_key_id, "POLYMARKET_KEY_ID"),
            _need(k.polymarket_secret_key, "POLYMARKET_SECRET_KEY"),
        )

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = _now_ms()
        sig = self._key.sign(f"{ts}{method}{path}".encode())
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": base64.b64encode(sig).decode(),
        }


def load_pem(text: str) -> ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey:
    """A PEM private key, tolerating one-line pastes and literal \\n sequences from .env files."""
    raw = text.strip().strip('"').replace("\\n", "\n")
    m = re.match(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", raw, re.S)
    if not m:
        raise ValueError("not a PEM private key")
    body = re.sub(r"\s", "", m.group(2))
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    loaded = serialization.load_pem_private_key(
        f"-----BEGIN {m.group(1)}-----\n{lines}\n-----END {m.group(1)}-----\n".encode(), password=None
    )
    if not isinstance(loaded, SUPPORTED_KEYS):
        raise ValueError(f"unsupported key type {type(loaded).__name__}")
    return loaded


class KalshiSigner:
    """Kalshi: Ed25519, or RSA-PSS/SHA-256, signature over timestamp + method + path."""

    def __init__(self, key_id: str, pem: str) -> None:
        self.key_id = key_id
        self._key = load_pem(pem)

    @classmethod
    def from_env(cls) -> "KalshiSigner":
        k = keys()
        key_id = _need(k.kalshi_key_id, "KALSHI_KEY_ID")
        if k.kalshi_private_key_path is not None:
            return cls(key_id, k.kalshi_private_key_path.expanduser().read_text())
        return cls(key_id, _need(k.kalshi_secret_key, "KALSHI_SECRET_KEY"))

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = _now_ms()
        msg = f"{ts}{method}{path}".encode()
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }
