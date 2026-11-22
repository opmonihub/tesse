"""Dynamic broker-aware asset discovery and alias canonicalization."""
from __future__ import annotations


def canonicalize_asset(symbol: str, aliases: dict[str, str] | None = None) -> str:
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("asset symbol is required")
    mapping = aliases or {}
    return mapping.get(symbol, symbol)


def discover_assets(assets, aliases: dict[str, str], *, mode_name: str = "OPTION", timeframe: str = "M1"):
    available = []
    skipped = {}
    canonical_values = set(aliases.values())
    for asset in assets:
        symbol = getattr(asset, "symbol", None)
        if not symbol:
            continue
        canonical = canonicalize_asset(symbol, aliases)
        if symbol not in aliases and canonical not in canonical_values:
            skipped[symbol] = "not_configured"
            continue
        mode = getattr(asset, "modes", {}).get(mode_name)
        if getattr(asset, "market_status", None) != "OPEN":
            skipped[symbol] = "unavailable"
            continue
        if mode is None or getattr(mode, "status", None) != "ACTIVE":
            skipped[symbol] = "unavailable"
            continue
        if timeframe not in getattr(mode, "timeframes", ()):
            skipped[symbol] = "unavailable"
            continue
        available.append(canonical)
    return sorted(set(available)), skipped
