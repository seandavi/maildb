# 0012 — Shared secrets in Secret Manager, device credentials in the Keychain

**Status:** accepted · **Date:** 2026-08-24
**Supersedes:** the Keychain-only provision in ADR 0010

## Context

An existing GCP project, `cdsci-infra`, already holds secrets in Google
Secret Manager and carries a configured OAuth consent screen. That makes
Secret Manager the obvious home for the OAuth *client* credentials.

It is not obviously the right home for everything. The two kinds of
credential here have different lifecycles:

- The **OAuth client id and secret** are shared configuration. Identical
  on every machine, rotated centrally, and useful to a second host.
- A **refresh token** is minted per device by a consent flow, is useless
  on another machine, and is read on every token refresh — including
  when the network is unreliable.

Putting refresh tokens in Secret Manager would make routine token
refresh depend on a GCP round-trip and on application-default
credentials being valid, adding a second failure mode to a path that is
supposed to be boring.

## Decision

Two backends, selected per secret kind:

```toml
[secrets]
backend       = "gsm"          # shared config secrets
gcp_project   = "cdsci-infra"
token_backend = "keyring"      # per-device credentials
```

- `secrets.backend` resolves the OAuth client credentials.
- `secrets.token_backend` resolves per-account refresh tokens.

Either may be set to the other value; the abstraction is one `SecretBackend`
protocol with two implementations, so a machine that wants everything
central can say so.

Config files hold references only. The loader matches values against
known credential shapes — `ya29.`, `1//`, `-----BEGIN … PRIVATE KEY`,
`AIza…` and friends — and refuses to start if one appears, so a pasted
token fails at load rather than at `git push`.

Reading from Secret Manager requires application-default credentials
(`gcloud auth application-default login`). That is a separate Google
identity from the Gmail OAuth grant, and confusing the two is the likely
support question.

## Consequences

- `google-cloud-secret-manager` joins the `gmail` extra.
- Token refresh stays local and works without GCP reachability.
- A second machine can reuse the OAuth client immediately but must run
  its own consent flow, which is the correct security property.
