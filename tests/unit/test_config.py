"""Configuration loading, and the refusal to hold secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from maildb.config import AccountConfig, assert_not_a_secret, load_config
from maildb.domain.errors import ConfigError, SecretInConfigError


def test_defaults_expand_user_paths() -> None:
    config = load_config(Path("/nonexistent/config.toml"))
    assert "~" not in str(config.database.path)
    assert config.database.path.is_absolute()


def test_loads_accounts_from_a_table_of_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [database]
        path = "/tmp/mail.db"

        [sync]
        retain_html = true

        [accounts.gmail-personal]
        provider = "gmail"
        email = "sean@example.com"
        auth_method = "oauth_installed"
        credential_ref = "maildb-gmail-personal"

        [accounts.cu]
        provider = "apple_mail"
        email = "sean@cuanschutz.edu"
        """
    )
    config = load_config(path)
    assert [a.id for a in config.accounts] == ["gmail-personal", "cu"]
    assert config.sync.retain_html is True
    assert config.account("cu").provider == "apple_mail"


@pytest.mark.parametrize(
    "value",
    [
        "1//0gArealLookingRefreshToken",
        "ya29.a0AfH6SMAAAAAAAAAAAAAA",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "-----BEGIN PRIVATE KEY-----",
        "xoxb-1234567890-abcdef",
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    ],
)
def test_credential_shaped_values_are_refused(value: str) -> None:
    """Config holds references. A pasted token fails at load, not at commit."""
    with pytest.raises(SecretInConfigError):
        assert_not_a_secret("accounts.x.credential_ref", value)


def test_reference_shaped_values_are_accepted() -> None:
    for value in ["maildb-gmail-personal", "projects/cdsci-infra/secrets/x", None]:
        assert_not_a_secret("accounts.x.credential_ref", value)


def test_service_account_cannot_target_a_personal_gmail_address() -> None:
    """Domain-wide delegation needs a Workspace admin console.

    A personal account has none, so no configuration makes this work --
    better to say so at load time than to fail obscurely against the API.
    """
    with pytest.raises(ConfigError, match="Workspace admin console"):
        AccountConfig.model_validate(
            {
                "id": "personal",
                "provider": "gmail",
                "email": "sean@gmail.com",
                "auth_method": "service_account",
                "service_account_file": "/tmp/key.json",
            }
        )


def test_service_account_is_fine_for_a_workspace_address() -> None:
    account = AccountConfig.model_validate(
        {
            "id": "work",
            "provider": "gmail",
            "email": "sean@somelab.org",
            "auth_method": "service_account",
            "service_account_file": "/tmp/key.json",
            "delegated_subject": "sean@somelab.org",
        }
    )
    assert account.delegated_subject == "sean@somelab.org"


def test_gmail_requires_an_auth_method() -> None:
    with pytest.raises(ConfigError, match="requires auth_method"):
        AccountConfig.model_validate(
            {"id": "g", "provider": "gmail", "email": "a@example.com"}
        )


def test_apple_mail_needs_no_auth_method() -> None:
    account = AccountConfig.model_validate(
        {"id": "cu", "provider": "apple_mail", "email": "sean@cuanschutz.edu"}
    )
    assert account.auth_method == "none"
