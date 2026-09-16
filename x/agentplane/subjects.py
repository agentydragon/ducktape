"""The subject an Agentplane policy binds to, defined once for every service that binds one.

The egress proxy and the Action Service authorize the same two kinds of caller -- a managed Sandbox,
or the ServiceAccount a workload's Pod runs as -- so they bind to one subject type rather than two
that have to be kept in agreement. `EgressBinding` and `ActionPolicyBinding` mirror this model's
JSON schema; `cluster/validation` pins each of them to it.

The wire shape is the way Kubernetes spells "exactly one of": a one-key object, `{sandbox: ...}` or
`{serviceAccount: ...}`, which a CRD constrains with `maxProperties: 1`. In Python that needs no
discriminator -- each variant requires a field the other forbids, so the union resolves itself, and
`extra="forbid"` is what refuses an object naming both.

A ServiceAccount is the identity that generalises: it is what a workload runs as whether or not
anything here provisioned it, so an agent hosted elsewhere is an ordinary subject rather than a
special case. A Sandbox subject only means anything for a workload whose lifecycle this cluster
owns, and is expected to retire once every such workload carries a ServiceAccount of its own.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Spec(BaseModel):
    """Operator-authored, so an unknown key is a mistake; constructed by field name in tests."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class SandboxRef(_Spec):
    """A Sandbox, optionally pinned to one instance of it.

    Absent `uid` is a defined state, not a missing value: the subject is that Sandbox by name,
    whichever instance currently holds it. Which of the two a binding may express is the CRD's to
    say -- `ActionPolicyBinding` requires the UID, because it matches on it and treats the name as
    documentation; `EgressBinding` does not, because a grant there dies with its Sandbox through the
    binding's owner reference.
    """

    name: str = Field(min_length=1)
    uid: str | None = Field(
        default=None, min_length=1, description="Pins one Sandbox instance; absent names the Sandbox whatever it is."
    )


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
