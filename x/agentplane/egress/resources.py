"""The resources the proxy reads off the API server, parsed once at the boundary.

`EgressPolicy`, `EgressBinding` and `EgressCredential` are Agentplane's own kinds (group
`agentplane.allegedly.works`, `v1alpha1`; the CRDs live in `cluster/k8s/agentplane-crds`).
A binding names its subjects as ServiceAccounts, and `Secret` holds the credentials the rules
substitute, in the credentials namespace. Only the fields the proxy reads are modelled; everything
else on the wire is ignored.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from typing import Annotated, Literal

from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from x.agentplane.subjects import ServiceAccountRef

POLICIES_PLURAL = "egresspolicies"
BINDINGS_PLURAL = "egressbindings"
CREDENTIALS_PLURAL = "egresscredentials"
SANDBOX_GROUP = "agents.x-k8s.io"
SANDBOX_VERSION = "v1beta1"
SANDBOX_KIND = "Sandbox"
SANDBOXES_PLURAL = "sandboxes"


class _Wire(BaseModel):
    """Read off the API server, constructed by field name in tests.

    The wire is camelCase and these fields are snake_case, so the alias is derived rather than
    spelled: a hand-written one is a second spelling of the field name that can disagree with it,
    and every field here is the plain camelCase of its own name.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True, alias_generator=to_camel)


class ObjectMeta(_Wire):
    name: str
    uid: str | None = Field(default=None, description="Set by the API server; absent on objects built by hand.")
    generation: int | None = Field(default=None, description="Bumped by the API server on every spec change.")
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Read by the app to tell a Flux-applied binding, which git owns, from a runtime "
        "grant it may revoke; the proxy decides nothing from them.",
    )


class SecretKeyRef(_Wire):
    name: str = Field(description="Secret in the proxy's namespace.")
    key: str = Field(description="Key of that Secret holding the credential.")


PLACEHOLDER_PREFIX = "agentplane-credential-"


def placeholder_of(credential_name: str) -> str:
    """What a sandbox sends where the credential goes. Derived and never authored, so one placeholder
    means one credential without anything having to check that two spellings agree; the separator is
    `-` and not `:` because a `:` cannot be a `basicUsername` component, which ends at the first one.
    """
    return f"{PLACEHOLDER_PREFIX}{credential_name}"


class TargetMethod(StrEnum):
    """How a client presents a credential in one header value. Each names a total parse of that
    value into the credential component and the text around it; adding a presentation means adding a
    method, never loosening one."""

    WHOLE_VALUE = "wholeValue"
    SCHEME_TOKEN = "schemeToken"
    BASIC_USERNAME = "basicUsername"
    BASIC_PASSWORD = "basicPassword"
    BASIC_WHOLE = "basicWhole"


class _TargetBase(_Wire):
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


Target = Annotated[
    WholeValueTarget | SchemeTokenTarget | BasicUsernameTarget | BasicPasswordTarget | BasicWholeTarget,
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
    hosts: list[str] = Field(min_length=1, description="Exact hosts, or `*.` suffix wildcards (`*.github.com`).")
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


class PolicySpec(_Wire):
    rules: list[Rule]


class EgressPolicy(_Wire):
    metadata: ObjectMeta
    spec: PolicySpec


class BindingSpec(_Wire):
    subjects: list[ServiceAccountRef]
    policies: list[str] = Field(
        description="EgressPolicy names in the same namespace; the order only breaks ties between rules "
        "that would decide alike."
    )
    expires_at: AwareDatetime | None = None


class ActiveReason(StrEnum):
    """Why a binding currently contributes rules in one replica's snapshot."""

    RESOLVED = "Resolved"
    EXPIRED = "Expired"
    MISSING_POLICY = "MissingPolicy"


class EgressBinding(_Wire):
    metadata: ObjectMeta
    spec: BindingSpec


class Secret(BaseModel):
    """One Secret's decoded data; never logged, never serialized."""

    model_config = ConfigDict(frozen=True)

    name: str
    data: dict[str, str] = Field(repr=False)

    @classmethod
    def from_v1(cls, secret: k8s_client.V1Secret) -> Secret:
        # `data` is base64 on the wire and the client leaves it so; `stringData` is write-only.
        return cls(
            name=secret.metadata.name,
            data={key: base64.b64decode(value).decode() for key, value in (secret.data or {}).items()},
        )
