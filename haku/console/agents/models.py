"""Canonical Haku Agent persistence vocabulary."""

from enum import StrEnum

MAX_AGENT_DISPLAY_NAME_LENGTH = 80


class ClientRegistrationKind(StrEnum):
    OAUTH_PROXY_UNCLASSIFIED = "oauth_proxy_unclassified"
    DCR = "dcr"
    CIMD = "cimd"
    PREREGISTERED = "preregistered"


class EnrollmentPhase(StrEnum):
    AWAITING_BROWSER = "awaiting_browser"
    AWAITING_APPROVAL = "awaiting_approval"
    ALLOWED = "allowed"
    EXCHANGING = "exchanging"
    COMPLETED = "completed"
    DENIED = "denied"
    EXPIRED = "expired"
    FAILED = "failed"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ABANDONED = "abandoned"
    DISABLED = "disabled"
    DELETED = "deleted"


class CredentialKind(StrEnum):
    OAUTH = "oauth"
    STATIC = "static"


class CredentialBindingStatus(StrEnum):
    ISSUING = "issuing"
    ISSUED = "issued"
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    FAILED = "failed"
