"""Exception types used by the CLI."""

from __future__ import annotations


class PrmptError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(PrmptError):
    """The CLI is not configured well enough to run."""


class ProviderError(PrmptError):
    """The model provider could not fulfill the request."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class OutputError(PrmptError):
    """The result could not be written or copied."""
