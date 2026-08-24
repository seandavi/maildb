"""Exception hierarchy.

Errors that reach an agent should say what to do differently; errors that
reach a user should say what broke.
"""

from __future__ import annotations


class MaildbError(Exception):
    """Base for every error this package raises."""


class ConfigError(MaildbError):
    """Configuration is missing, malformed, or unsafe."""


class SecretInConfigError(ConfigError):
    """A value that looks like a credential was found in the config file.

    Config holds *references* to secrets, never secrets. Raised at load
    time so a leaked token is caught before it can be committed.
    """


class MigrationError(MaildbError):
    """Schema migration failed or the database is at an unexpected version."""


class QueryError(MaildbError):
    """Base for query-layer failures."""


class QueryRejected(QueryError):
    """The query was refused before execution.

    Carries the layer that refused it, so the tests can prove the
    authorizer alone is sufficient (see decisions/0002).
    """

    def __init__(self, message: str, *, layer: str) -> None:
        super().__init__(message)
        self.layer = layer


class QueryTimeout(QueryError):
    """The query exceeded its execution budget and was interrupted."""


class ResponseTooLarge(QueryError):
    """The result set exceeded the configured response size cap."""


class ProviderError(MaildbError):
    """A provider failed to satisfy a request."""


class CapabilityUnsupported(ProviderError):
    """The provider cannot do this, and says so rather than failing oddly."""

    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(f"provider {provider!r} does not support {capability!r}")
        self.provider = provider
        self.capability = capability
