"""Thin CLI wrapper. The logic lives in signals.seeding so tests can import it."""

from __future__ import annotations

import sys

from signals.__main__ import cmd_seed

if __name__ == "__main__":
    sys.exit(cmd_seed())
