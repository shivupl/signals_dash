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


async def current_flag_threshold() -> int:
    """What the worker is pushing and price-stamping at, right now.

    Stored settings win over the environment, because the panel can change one and
    cannot change the other. Lives here rather than in a route module so the feed
    and the meta routes can both anchor to it without importing each other.
    """
    stored = await get_store().get_setting("flag_threshold")
    return int(stored) if stored is not None else get_settings().flag_threshold
