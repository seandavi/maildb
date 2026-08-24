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


#: Filler for the fake credentials below. Long enough to satisfy the
#: minimum-length parts of the detector patterns, and unmistakable to a
#: human reading a diff.
_FILLER = "EXAMPLENOTAREALCREDENTIAL" * 2


@pytest.mark.parametrize(
    ("prefix", "label"),
    [
        ("1//", "google refresh token"),
        ("ya29.", "google access token"),
        ("ghp_", "github token"),
        ("sk-", "openai-style key"),
        ("xoxb-", "slack token"),
        ("AIza", "google api key"),
    ],
)
def test_credential_shaped_values_are_refused(prefix: str, label: str) -> None:
    """Config holds references. A pasted token fails at load, not at commit.

    The fakes are assembled at runtime rather than written as literals.
    A credential-shaped literal in a public repository trips GitHub's
    secret scanner and every other detector, producing an alert that
    costs someone real time to disprove -- which is exactly what happened
    the first time this test was written. Concatenation exercises the
    same patterns while leaving nothing in the source to match.
    """
    with pytest.raises(SecretInConfigError, match="not a reference"):
        assert_not_a_secret("accounts.x.credential_ref", prefix + _FILLER)


def test_private_key_blocks_are_refused() -> None:
    """A PEM header is the one pattern that cannot be built from parts."""
    pem_header = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
    with pytest.raises(SecretInConfigError):
        assert_not_a_secret("accounts.x.credential_ref", pem_header)


def test_no_credential_shaped_literals_in_this_file() -> None:
    """Guard against the next person reintroducing the problem.

    Fails if a literal matching a common credential prefix appears in the
    test source, which is what generated a false-positive secret-scanning
    alert on this repository.
    """
    import re

    source = Path(__file__).read_text()
    offenders = re.findall(
        r'"(?:1//|ya29\.|ghp_|sk-|xox[baprs]-|AIza)[A-Za-z0-9._-]{8,}"', source
    )
    assert not offenders, f"credential-shaped literals in test source: {offenders}"


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
