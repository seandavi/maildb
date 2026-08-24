"""Wiring a configured account to a live provider.

The one place that knows how config, secrets, and providers fit together,
so the CLI, daemon, and MCP server do not each reinvent it.
"""

from __future__ import annotations

from typing import Any

from maildb.config import AccountConfig, Config
from maildb.domain.enums import AuthMethod, Provider
from maildb.domain.errors import ConfigError, ProviderError
from maildb.providers.gmail import (
    SCOPES_READONLY,
    GmailAuth,
    GmailProvider,
)
from maildb.secrets import KeyringBackend, get_backend


def _shared_secret(config: Config, ref: str | None) -> str | None:
    if not ref:
        return None
    backend = get_backend(config.secrets.backend, config.secrets.gcp_project)
    return backend.get(ref)


def build_auth(
    config: Config, account: AccountConfig, *, scopes: list[str] | None = None
) -> GmailAuth:
    """Construct the auth seam for an account from its secret references."""
    return GmailAuth(
        account.id,
        scopes=scopes or SCOPES_READONLY,
        client_id=_shared_secret(config, account.oauth_client_id_ref),
        client_secret=_shared_secret(config, account.oauth_client_secret_ref),
        service_account_file=(
            str(account.service_account_file) if account.service_account_file else None
        ),
        delegated_subject=account.delegated_subject,
        redirect_port=account.oauth_redirect_port,
    )


def token_store(config: Config) -> KeyringBackend:
    backend = get_backend(config.secrets.token_backend)
    if not isinstance(backend, KeyringBackend):
        raise ConfigError(
            "token storage must be a writable backend; set "
            "secrets.token_backend = 'keyring'"
        )
    return backend


def stored_token(config: Config, account: AccountConfig) -> str | None:
    ref = account.credential_ref or account.id
    try:
        return token_store(config).get(ref)
    except ConfigError:
        return None


def save_token(config: Config, account: AccountConfig, token: str) -> None:
    token_store(config).set(account.credential_ref or account.id, token)


def authorize(
    config: Config, account: AccountConfig, *, scopes: list[str] | None = None
) -> Any:
    """Obtain credentials, running the consent flow only if necessary."""
    auth = build_auth(config, account, scopes=scopes)

    if account.auth_method is AuthMethod.SERVICE_ACCOUNT:
        return auth.service_account_credentials()

    credentials = auth.installed_app_credentials(stored_token(config, account))
    # Persist on every call: a refresh mints a new access token, and the
    # stored blob carries it.
    save_token(config, account, GmailAuth.to_stored_token(credentials))
    return credentials


def build_provider(
    config: Config, account: AccountConfig, *, scopes: list[str] | None = None
) -> GmailProvider:
    """Return a ready-to-use provider for a configured account."""
    if account.provider is not Provider.GMAIL:
        raise ProviderError(
            f"provider {account.provider} is not implemented yet "
            "(Apple Mail lands in M5)"
        )

    from googleapiclient.discovery import build

    credentials = authorize(config, account, scopes=scopes)
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return GmailProvider(account.id, service, retain_html=config.sync.retain_html)
