"""Exception types used by the CLI."""


class PrmptError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(PrmptError):
    """The CLI is not configured well enough to run."""


class ProviderError(PrmptError):
    """The model provider could not fulfill the request."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
