"""The resources the proxy reads off the API server, parsed once at the boundary.

`EgressPolicy`, `EgressBinding` and `EgressCredential` are Agentplane's own kinds (group
`agentplane.allegedly.works`, `v1alpha1`; the CRDs live in `cluster/k8s/agentplane-crds`).
`Sandbox` is the subject kind the bindings name, and `Secret` holds the credentials the rules
substitute, in the credentials namespace. Only the fields the proxy reads are modelled; everything
else on the wire is ignored.
"""

from __future__ import annotations

import base64
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
POLICIES_PLURAL = "egresspolicies"
BINDINGS_PLURAL = "egressbindings"
CREDENTIALS_PLURAL = "egresscredentials"
SANDBOX_GROUP = "agents.x-k8s.io"
SANDBOX_VERSION = "v1beta1"
SANDBOXES_PLURAL = "sandboxes"
SANDBOX_KIND = "Sandbox"


class _Wire(BaseModel):
    """Read off the API server (camelCase aliases), constructed by field name in tests."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class ObjectMeta(_Wire):
    name: str
    uid: str | None = Field(default=None, description="Set by the API server; absent on objects built by hand.")
    generation: int | None = Field(default=None, description="Bumped by the API server on every spec change.")


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


class CredentialSource(_Wire):
    """Where the real value comes from. One source exists; the object shape admits others later."""

    secret_ref: SecretKeyRef = Field(alias="secretRef")


class CredentialSpec(_Wire):
    source: CredentialSource
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
    credential_ref: CredentialRef | None = Field(default=None, alias="credentialRef")


class PolicySpec(_Wire):
    rules: list[Rule]


class EgressPolicy(_Wire):
    metadata: ObjectMeta
    spec: PolicySpec


class SandboxRef(_Wire):
    name: str


class Subject(_Wire):
    sandbox: SandboxRef


class BindingSpec(_Wire):
    subjects: list[Subject]
    policies: list[str] = Field(
        description="EgressPolicy names in the same namespace; the order only breaks ties between rules "
        "that would decide alike."
    )
    expires_at: AwareDatetime | None = Field(default=None, alias="expiresAt")


class ConditionStatus(StrEnum):
    TRUE = "True"
    FALSE = "False"


class ActiveReason(StrEnum):
    """Why the `Active` condition holds or does not; one per non-granting state."""

    RESOLVED = "Resolved"
    EXPIRED = "Expired"
    MISSING_POLICY = "MissingPolicy"


ACTIVE_CONDITION = "Active"


class Condition(_Wire):
    """The standard `metav1.Condition` shape."""

    type: str
    status: ConditionStatus
    reason: str
    message: str = ""
    last_transition_time: datetime = Field(alias="lastTransitionTime")
    observed_generation: int | None = Field(default=None, alias="observedGeneration")


class BindingStatus(_Wire):
    observed_generation: int | None = Field(default=None, alias="observedGeneration")
    conditions: list[Condition] = Field(default_factory=list)
    resolved_policies: int = Field(default=0, alias="resolvedPolicies")


class EgressBinding(_Wire):
    metadata: ObjectMeta
    spec: BindingSpec
    status: BindingStatus | None = None


class Sandbox(_Wire):
    metadata: ObjectMeta


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
