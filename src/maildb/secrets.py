"""Secret retrieval.

Config files hold *references* to secrets, never secrets. Two backends:

``gsm``
    Google Secret Manager in the ``cdsci-infra`` project. Authoritative
    for shared configuration — notably the OAuth client secret, which is
    the same on every machine and benefits from central rotation.
    Requires application-default credentials
    (``gcloud auth application-default login``).

``keyring``
    The macOS Keychain. Right for per-device credentials — notably OAuth
    refresh tokens, which are minted per machine, are useless elsewhere,
    and should not require a GCP round-trip on every token refresh.

Both are optional imports so the core package stays installable without
the Gmail extra.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from maildb.domain.errors import ConfigError

KEYRING_SERVICE = "maildb"


class SecretBackend(Protocol):
    def get(self, ref: str) -> str: ...


class GoogleSecretManager:
    """Reads secrets from Google Secret Manager.

    ``ref`` is a bare secret name (``maildb-gmail-oauth-client``), resolved
    against the configured project at the ``latest`` version, or a fully
    qualified resource name if it starts with ``projects/``.
    """

    def __init__(self, project: str) -> None:
        self.project = project

    def get(self, ref: str) -> str:
        try:
            from google.cloud import secretmanager
        except ImportError as exc:  # pragma: no cover - import guard
            raise ConfigError(
                "google-cloud-secret-manager is not installed; "
                "install the 'gmail' extra or use the keyring backend"
            ) from exc

        name = (
            ref
            if ref.startswith("projects/")
            else (f"projects/{self.project}/secrets/{ref}/versions/latest")
        )
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(request={"name": name})
        payload: str = response.payload.data.decode("utf-8")
        return payload


class KeyringBackend:
    """Reads secrets from the OS keychain."""

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service

    def get(self, ref: str) -> str:
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - import guard
            raise ConfigError(
                "keyring is not installed; install the 'gmail' extra"
            ) from exc

        value = keyring.get_password(self.service, ref)
        if value is None:
            raise ConfigError(
                f"no keychain entry {ref!r} under service {self.service!r}"
            )
        return value

    def set(self, ref: str, value: str) -> None:
        import keyring

        keyring.set_password(self.service, ref, value)


@lru_cache(maxsize=8)
def get_backend(kind: str, project: str | None = None) -> SecretBackend:
    """Resolve a backend by name.

    Cached: constructing a Secret Manager client is not free, and the
    daemon resolves the same handful of refs repeatedly.
    """
    if kind == "gsm":
        if not project:
            raise ConfigError("the 'gsm' secret backend requires secrets.gcp_project")
        return GoogleSecretManager(project)
    if kind == "keyring":
        return KeyringBackend()
    raise ConfigError(f"unknown secret backend: {kind!r}")
