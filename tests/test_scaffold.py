"""Proves the harness itself works before anything depends on it."""

from __future__ import annotations

import socket

import pytest


def test_package_imports() -> None:
    import signals

    assert signals.__name__ == "signals"


def test_network_is_disabled() -> None:
    """The kill-switch must actually bite, or every other test's isolation is a lie."""
    with pytest.raises(Exception):  # noqa: B017 -- pytest_socket raises its own type
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("93.184.216.34", 80))
