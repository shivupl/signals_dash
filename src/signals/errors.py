"""Error taxonomy. The distinction the runner cares about is retry vs. give up."""

from __future__ import annotations


class SignalsError(Exception):
    pass


class TransientSourceError(SignalsError):
    """A source is temporarily unhappy: timeout, 5xx, malformed single response.

    The runner backs off and tries again. One source failing must never stop the
    others.
    """


class PermanentSourceError(SignalsError):
    """The source or our use of it is wrong in a way retrying will not fix."""


class RateLimited(TransientSourceError):
    """Explicit throttling. Carries the server's Retry-After when it gave one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
