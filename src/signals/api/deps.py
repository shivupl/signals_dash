"""Dependency wiring, kept separate so tests can override the store."""

from __future__ import annotations

from ..config import Settings
from ..prices import PriceService
from ..store.pg import PgStore

_store: PgStore | None = None
_settings: Settings | None = None
_prices: PriceService | None = None


def set_store(store: PgStore | None) -> None:
    global _store
    _store = store


def set_settings(settings: Settings | None) -> None:
    global _settings
    _settings = settings


def set_prices(prices: PriceService | None) -> None:
    global _prices
    _prices = prices


def get_prices() -> PriceService | None:
    """None is a legitimate answer: with no provider the chart simply stays empty."""
    return _prices


def get_store() -> PgStore:
    if _store is None:
        raise RuntimeError("store is not configured")
    return _store


def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("settings are not configured")
    return _settings
