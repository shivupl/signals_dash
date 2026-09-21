"""Settings, read from the environment in exactly one place."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    sec_user_agent: str
    database_url: str
    redis_url: str
    flag_threshold: int
    anthropic_api_key: str | None
    #: Guards the settings endpoint. Unset means settings are read-only.
    admin_token: str | None = None

    @classmethod
    def from_env(
        cls, env: dict[str, str] | None = None, *, require_sec_user_agent: bool = True
    ) -> Settings:
        """Read settings from the environment.

        Only components that actually call SEC require the user agent. The API
        reads from Postgres and never leaves the machine, so making it refuse to
        start without a contact address would be a coupling with no safety value.
        """
        e = os.environ if env is None else env
        ua = e.get("SEC_USER_AGENT", "").strip()
        if not require_sec_user_agent and not ua:
            ua = ""
        # Fail here, loudly, rather than as an unexplained 403 an hour into a
        # trading day. SEC rejects every request without a declared contact.
        elif not ua:
            raise ConfigError(
                "SEC_USER_AGENT is unset. SEC returns 403 for requests without a "
                "declared User-Agent carrying a real contact address. "
                'Set it in .env, e.g. SEC_USER_AGENT="Signals/0.1 (you@example.com)"'
            )
        if ua and "@" not in ua:
            raise ConfigError(
                f"SEC_USER_AGENT={ua!r} has no contact address. SEC's fair-access "
                'policy expects "AppName/Version (you@example.com)".'
            )
        return cls(
            sec_user_agent=ua,
            database_url=e.get("DATABASE_URL", "postgresql://postgres:dev@postgres:5432/signals"),
            redis_url=e.get("REDIS_URL", "redis://redis:6379/0"),
            flag_threshold=int(e.get("FLAG_THRESHOLD", "30")),
            anthropic_api_key=(e.get("ANTHROPIC_API_KEY") or "").strip() or None,
            admin_token=(e.get("ADMIN_TOKEN") or "").strip() or None,
        )
