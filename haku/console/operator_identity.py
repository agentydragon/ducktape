"""Canonical Operator identity types and exact OIDC trust-domain matching."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class OperatorStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class IdentityAnchorKey:
    trust_domain: str
    stable_external_user_key: str


@dataclass(frozen=True, slots=True)
class VerifiedExternalIdentity:
    """An issuer-scoped subject after a boundary-specific verifier has authenticated it.

    This Haku-core value deliberately says nothing about Authlib, FastMCP, access tokens, or ID
    tokens. Each protocol adapter may construct it only after completing its own verification.
    """

    issuer: str
    subject: str


@dataclass(frozen=True, slots=True)
class ResolvedOperatorIdentity:
    operator_id: UUID
    identity_id: UUID


class OperatorIdentityError(Exception):
    """Base class for canonical identity resolution failures."""


class UntrustedOidcIssuerError(OperatorIdentityError):
    """The verified principal did not come from this trust domain's exact issuer allowlist."""


class InactiveOperatorError(OperatorIdentityError):
    """The identity resolves to a disabled or missing Operator."""


class OperatorIdentityInvariantError(OperatorIdentityError):
    """Persisted identity rows contradict the canonical trust-domain mapping."""


@dataclass(frozen=True, slots=True)
class OperatorIdentityTrust:
    """Map verified OIDC principals into one explicit Authentik user-id namespace."""

    trust_domain: str
    trusted_issuers: frozenset[str]

    def anchor_key(self, identity: VerifiedExternalIdentity) -> IdentityAnchorKey:
        if identity.issuer not in self.trusted_issuers:
            raise UntrustedOidcIssuerError(f"OIDC issuer is not trusted for {self.trust_domain!r}")
        if not identity.subject.strip():
            raise OperatorIdentityInvariantError("verified OIDC subject must not be empty")
        return IdentityAnchorKey(trust_domain=self.trust_domain, stable_external_user_key=identity.subject)

    def configured_anchor_key(self, stable_external_user_key: str) -> IdentityAnchorKey:
        if not stable_external_user_key.strip():
            raise OperatorIdentityInvariantError("configured stable external user key must not be empty")
        return IdentityAnchorKey(trust_domain=self.trust_domain, stable_external_user_key=stable_external_user_key)
