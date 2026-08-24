"""Configuration loading.

Sources, in increasing precedence: defaults, the TOML config file,
``MAILDB_*`` environment variables.

The loader actively refuses to start when a value looks like a
credential. Config holds references — a Secret Manager secret name, a
Keychain entry name, a path to a key file — and a pasted token is a
mistake worth failing loudly on, before it reaches a commit.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maildb.domain.enums import AuthMethod, Provider
from maildb.domain.errors import ConfigError, SecretInConfigError

DEFAULT_CONFIG_PATH: Final = Path("~/.config/maildb/config.toml")
DEFAULT_DB_PATH: Final = Path("~/.local/share/maildb/mail.db")

#: Shapes that indicate an actual credential rather than a reference.
_SECRET_PATTERNS: Final = (
    re.compile(r"^ya29\."),  # google access token
    re.compile(r"^1//"),  # google refresh token
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{20,}"),  # github token
    re.compile(r"^sk-[A-Za-z0-9]{20,}"),  # openai-style key
    re.compile(r"^xox[baprs]-"),  # slack token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r'"private_key"\s*:'),  # inlined service-account json
    re.compile(r"^AIza[0-9A-Za-z_-]{30,}"),  # google api key
)


def _expand(path: Path) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser()


def assert_not_a_secret(field: str, value: str | None) -> None:
    """Raise if ``value`` looks like a credential rather than a reference."""
    if not value:
        return
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            raise SecretInConfigError(
                f"{field} looks like a credential, not a reference. "
                "Store the secret in Secret Manager or the Keychain and put "
                "its name here instead."
            )


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(validate_default=True)

    path: Path = DEFAULT_DB_PATH

    @field_validator("path")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        return _expand(value)


class SecretsConfig(BaseModel):
    """Where credentials live. See decisions/0012."""

    #: Backend for shared configuration secrets (the OAuth client secret).
    backend: str = "gsm"
    #: GCP project holding those secrets.
    gcp_project: str | None = "cdsci-infra"
    #: Backend for per-device credentials (OAuth refresh tokens).
    token_backend: str = "keyring"


class SyncConfig(BaseModel):
    interval_seconds: int = 300
    #: HTML bodies dominate database size and are never full-text indexed.
    #: Off by default (ADR 0008).
    retain_html: bool = False
    #: Reconciliation is how deletions and flag changes are detected for
    #: providers with no change feed (ADR 0005). Not optional.
    reconcile_interval_seconds: int = 3600
    backfill_since: str | None = None


class QueryLimits(BaseModel):
    max_rows: int = 1000
    max_execution_seconds: float = 5.0
    max_response_bytes: int = 1_048_576
    #: Only ever False in tests, to prove the authorizer alone is
    #: sufficient (ADR 0002). Never disable this in production.
    parser_enabled: bool = True


class ServerConfig(BaseModel):
    model_config = ConfigDict(validate_default=True)

    bind: str = "127.0.0.1"
    port: int = 8787
    token_file: Path = Path("~/.local/share/maildb/token")

    @field_validator("token_file")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        return _expand(value)


class AccountConfig(BaseModel):
    id: str
    provider: Provider
    email: str
    enabled: bool = True
    sync_enabled: bool = True

    auth_method: AuthMethod = AuthMethod.NONE

    #: Reference to the OAuth client (id + secret) in the shared backend.
    oauth_client_ref: str | None = None
    #: Reference to this account's refresh token in the token backend.
    credential_ref: str | None = None
    #: Path to a service-account key file. Path only, never the key.
    service_account_file: Path | None = None
    #: Impersonation target for domain-wide delegation. Workspace only.
    delegated_subject: str | None = None

    @model_validator(mode="after")
    def _check(self) -> AccountConfig:
        for field in (
            "oauth_client_ref",
            "credential_ref",
            "delegated_subject",
        ):
            assert_not_a_secret(f"accounts.{self.id}.{field}", getattr(self, field))

        if self.provider is Provider.GMAIL:
            if self.auth_method is AuthMethod.NONE:
                raise ConfigError(
                    f"accounts.{self.id}: gmail requires auth_method "
                    "'oauth_installed' or 'service_account'"
                )
            if self.auth_method is AuthMethod.SERVICE_ACCOUNT:
                if self.service_account_file is None:
                    raise ConfigError(
                        f"accounts.{self.id}: service_account requires "
                        "service_account_file"
                    )
                if self.email.endswith("@gmail.com"):
                    # Domain-wide delegation is authorized in a Workspace
                    # admin console. A personal account has none, so no
                    # key configuration can make this work.
                    raise ConfigError(
                        f"accounts.{self.id}: a service account cannot access a "
                        "personal @gmail.com address -- domain-wide delegation "
                        "requires a Workspace admin console. Use "
                        "auth_method = 'oauth_installed'."
                    )
        return self


class Config(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    query: QueryLimits = Field(default_factory=QueryLimits)
    server: ServerConfig = Field(default_factory=ServerConfig)
    accounts: list[AccountConfig] = Field(default_factory=list)

    def account(self, account_id: str) -> AccountConfig:
        for account in self.accounts:
            if account.id == account_id:
                return account
        raise ConfigError(f"no such account: {account_id!r}")


def _apply_env(data: dict[str, Any]) -> dict[str, Any]:
    """Overlay ``MAILDB_<SECTION>_<KEY>`` environment variables."""
    for key, value in os.environ.items():
        if not key.startswith("MAILDB_"):
            continue
        parts = key.removeprefix("MAILDB_").lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, field = parts
        if section in {"database", "secrets", "sync", "query", "server"}:
            data.setdefault(section, {})[field] = value
    return data


def load_config(path: Path | None = None) -> Config:
    """Load configuration, or return defaults when no file exists."""
    env_path = os.environ.get("MAILDB_CONFIG")
    config_path = _expand(path or Path(env_path or DEFAULT_CONFIG_PATH))

    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)

    # Accounts are a table-of-tables in TOML; the id is the table key.
    accounts = data.pop("accounts", {})
    if isinstance(accounts, dict):
        data["accounts"] = [
            {"id": account_id, **fields} for account_id, fields in accounts.items()
        ]
    else:
        data["accounts"] = accounts

    return Config.model_validate(_apply_env(data))
