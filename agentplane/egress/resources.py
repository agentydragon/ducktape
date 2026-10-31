"""Python read models for the app's egress policy and credential projections.

Go API types own CRD generation, and the Go proxy owns parsing and enforcement.
These models describe the fields the Python app presents; cdk8s also uses the
credential-name-to-placeholder convention defined here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

CREDENTIALS_PLURAL = "egresscredentials"


class _Wire(BaseModel):
    """Read off the API server, constructed by field name in tests.

    The wire is camelCase and these fields are snake_case, so the alias is derived rather than
    spelled: a hand-written one is a second spelling of the field name that can disagree with it,
    and every field here is the plain camelCase of its own name.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True, alias_generator=to_camel)


class ObjectMeta(_Wire):
    name: str


class SecretKeyRef(_Wire):
    name: str = Field(description="Secret in the egress credentials namespace.")
    key: str = Field(description="Key of that Secret holding the credential.")


PLACEHOLDER_PREFIX = "agentplane-credential-"


def placeholder_of(credential_name: str) -> str:
    """What a sandbox sends where the credential goes. Derived and never authored, so one placeholder
    means one credential without anything having to check that two spellings agree; the separator is
    `-` and not `:` because a `:` cannot be a `basicUsername` component, which ends at the first one.
    """
    return f"{PLACEHOLDER_PREFIX}{credential_name}"


class TargetMethod(StrEnum):
    """How a client presents a credential in a header component or declared JSON field. Each names a total parse of that
    value into the credential component and the text around it; adding a presentation means adding a
    method, never loosening one."""

    WHOLE_VALUE = "wholeValue"
    SCHEME_TOKEN = "schemeToken"
    BASIC_USERNAME = "basicUsername"
    BASIC_PASSWORD = "basicPassword"
    BASIC_WHOLE = "basicWhole"
    JSON_FIELD = "jsonField"


class _TargetBase(_Wire):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    header: str = Field(min_length=1, description="Request header this presentation puts the credential in.")


class WholeValueTarget(_TargetBase):
    """`<credential>` — the header value is the credential and nothing else."""

    method: Literal[TargetMethod.WHOLE_VALUE]


class SchemeTokenTarget(_TargetBase):
    """`<scheme> <credential>`."""

    method: Literal[TargetMethod.SCHEME_TOKEN]
    scheme: str = Field(
        min_length=1, description="Scheme the value must carry, compared case-insensitively (`Bearer`)."
    )


class BasicUsernameTarget(_TargetBase):
    """`Basic base64(<credential>:<password>)`."""

    method: Literal[TargetMethod.BASIC_USERNAME]


class BasicPasswordTarget(_TargetBase):
    """`Basic base64(<username>:<credential>)`."""

    method: Literal[TargetMethod.BASIC_PASSWORD]


class BasicWholeTarget(_TargetBase):
    """`Basic base64(<credential>)` — a payload that is the credential, colon or no colon."""

    method: Literal[TargetMethod.BASIC_WHOLE]


class JsonFieldTarget(_Wire):
    """An exact string value in one top-level JSON object field; no paths or templates."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    method: Literal[TargetMethod.JSON_FIELD]
    field: str = Field(
        min_length=1,
        max_length=256,
        description="Top-level JSON field whose entire string value must equal the placeholder.",
    )


# Public presentation syntax, shared by resource parsing and both API projections. Keep
# credential values/source metadata out of these variants; those belong to CredentialSpec.
Target = Annotated[
    WholeValueTarget
    | SchemeTokenTarget
    | BasicUsernameTarget
    | BasicPasswordTarget
    | BasicWholeTarget
    | JsonFieldTarget,
    Field(discriminator="method"),
]


class AuthenticatedWorkloadTokenSource(_Wire):
    """The bearer retained from this request's successful workload authentication."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class ProjectedWorkloadTokenSource(_Wire):
    """A token for another audience, projected into the sidecar and presented on the hop.

    The hop bearer proves the caller to this proxy and carries the proxy's own audience, so a
    destination validating its own -- the API server against `--api-audiences` above all -- refuses
    it. This source is the same Pod's identity minted for that destination instead: the sidecar
    holds it, the workload never does, and the proxy substitutes it only after reviewing it as the
    very Pod that authenticated.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    audience: str = Field(
        min_length=1,
        description="Audience of the projected token to substitute, as the destination validates it "
        "and as the sidecar's projection requests it.",
    )


class CredentialSource(_Wire):
    """Exactly one tagged source for the value substituted at a declared target."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    secret_ref: SecretKeyRef | None = None
    authenticated_workload_token: AuthenticatedWorkloadTokenSource | None = None
    projected_workload_token: ProjectedWorkloadTokenSource | None = None

    @model_validator(mode="after")
    def _one_source(self) -> CredentialSource:
        sources = (self.secret_ref, self.authenticated_workload_token, self.projected_workload_token)
        if sum(source is not None for source in sources) != 1:
            raise ValueError(
                "source must set exactly one of secretRef, authenticatedWorkloadToken or projectedWorkloadToken"
            )
        return self


class CredentialSpec(_Wire):
    source: CredentialSource
    description: str = Field(
        min_length=1,
        description="What this credential is and what it can do, in prose, for the sandbox that will "
        "present it without ever seeing it. Read by agents, so it is not a place for anything secret.",
    )
    targets: list[Target] = Field(min_length=1, description="Every exact location this credential may be presented in.")


class EgressCredential(_Wire):
    """A credential a sandbox may present without holding, named so that its placeholder is unique."""

    metadata: ObjectMeta
    spec: CredentialSpec

    @property
    def placeholder(self) -> str:
        return placeholder_of(self.metadata.name)


class CredentialRef(_Wire):
    name: str = Field(description="EgressCredential in the same namespace as the policy.")


class Rule(_Wire):
    hosts: list[Annotated[str, Field(max_length=256)]] = Field(
        min_length=1,
        max_length=256,
        description="Exact hosts, `*.` suffix wildcards (`*.github.com`), or `*` for any public destination.",
    )
    methods: list[str] | None = Field(default=None, description="HTTP methods; absent admits any.")
    paths: list[str] | None = Field(
        default=None, description="Path globs: `*` within one segment, `**` across segments; absent admits any."
    )
    credential_ref: CredentialRef | None = None
    cluster_internal: bool = Field(
        default=False,
        description="This rule's hosts are inside the cluster and meant to be, so the proxy's refusal "
        "of private addresses does not apply to them. Off by default: that refusal is what stops an "
        "admitted name from resolving into the cluster, DNS rebinding included.",
    )

    @model_validator(mode="after")
    def _blanket_is_public_and_credentialless(self) -> Rule:
        if "*" in self.hosts and (self.cluster_internal or self.credential_ref is not None):
            raise ValueError("A blanket host rule cannot set clusterInternal or credentialRef")
        return self
