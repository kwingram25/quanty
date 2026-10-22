"""Small parsing helpers shared by feed adapters."""

from datetime import datetime


def iso_ms(s: str) -> int:
    """Epoch ms from an ISO-8601 UTC timestamp with any number of fractional digits (Coinbase sends 9)."""
    base, _, frac = s.rstrip("Z").partition(".")
    whole = datetime.fromisoformat(base + "+00:00")
    return int(whole.timestamp()) * 1000 + int((frac + "000")[:3])
