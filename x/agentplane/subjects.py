"""The subject an Agentplane policy binds to, defined once for every service that binds one.

The egress proxy and the Action Service authorize the same two kinds of caller -- a managed Sandbox,
or the ServiceAccount a workload's Pod runs as -- so they bind to one subject type rather than two
that have to be kept in agreement. `EgressBinding` and `ActionPolicyBinding` mirror this model's
JSON schema; `cluster/validation` pins each of them to it.

The wire shape is the way Kubernetes spells "exactly one of": a one-key object, `{sandbox: ...}` or
`{serviceAccount: ...}`, which a CRD constrains with `maxProperties: 1`. In Python that needs no
discriminator -- each variant requires a field the other forbids, so the union resolves itself, and
`extra="forbid"` is what refuses an object naming both.

A Sandbox is named with its UID, so a grant is to that Sandbox and not to its name: a Sandbox
deleted and recreated under the same name does not inherit what the old one was granted. Cascading
deletion of the binding object is housekeeping on top of that, not the guarantee itself.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Spec(BaseModel):
    """Operator-authored, so an unknown key is a mistake; constructed by field name in tests."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class SandboxRef(_Spec):
    name: str = Field(min_length=1)
    uid: str = Field(min_length=1, description="Pins the Sandbox instance; a binding whose Sandbox is gone is inert.")


class ServiceAccountRef(_Spec):
    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)


class SandboxSubject(_Spec):
    sandbox: SandboxRef


class ServiceAccountSubject(_Spec):
    """A workload no Sandbox owns, named by the ServiceAccount its Pod runs as.

    Unlike a Sandbox subject this is not lifecycle-bound: every Pod running as that ServiceAccount is
    this subject, so a binding is only as narrow as the ServiceAccount is dedicated to one workload.
    """

    service_account: ServiceAccountRef = Field(alias="serviceAccount")


type Subject = SandboxSubject | ServiceAccountSubject


class SubjectKind(StrEnum):
    """Spelled as the CRDs spell it, so one vocabulary covers the wire, the API and the UI."""

    SANDBOX = "Sandbox"
    SERVICE_ACCOUNT = "ServiceAccount"


class SubjectView(BaseModel):
    """One subject as an API reports it, shaped as Kubernetes shapes a RoleBinding's subject.

    A name alone is ambiguous -- a Sandbox and a ServiceAccount can share one and remain two
    different subjects -- so the kind travels beside it rather than being encoded into the string.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SubjectKind
    name: str


def subject_view(subject: Subject) -> SubjectView:
    if isinstance(subject, SandboxSubject):
        return SubjectView(kind=SubjectKind.SANDBOX, name=subject.sandbox.name)
    return SubjectView(kind=SubjectKind.SERVICE_ACCOUNT, name=subject.service_account.name)
