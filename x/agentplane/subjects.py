"""The subject vocabulary the egress proxy and the Action Service both decide against.

Both bind policy to the same two kinds -- a managed Sandbox, or the ServiceAccount a workload's Pod
runs as -- and both spell that on the wire the way Kubernetes spells "exactly one of": a one-key
object, `{sandbox: ...}` or `{serviceAccount: ...}`, which a CRD constrains with `maxProperties: 1`.

What each side's *reference* carries differs and should: the Action Service is multi-namespace and
pins a Sandbox by UID, where the egress proxy serves one namespace and re-checks the UID when it
authenticates. What must not differ is the vocabulary -- the kind names, how an API reports a
subject, and which key decides the union -- so those live here and neither service redefines them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

SANDBOX_KEY = "sandbox"
SERVICE_ACCOUNT_KEY = "serviceAccount"


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


def subject_kind(value: Any) -> str | None:
    """Which one-key form a subject takes, for Pydantic's `Discriminator`.

    Discrimination runs before validation, so this sees whatever the caller passed: a raw object off
    the API server, keyed by alias; the same object keyed by field name, as tests construct it; or an
    already-constructed variant. All three answer the same question -- which of the two keys is
    present -- so this reads the key rather than a list of variant classes that each service would
    have to keep in step with this function.
    """
    if isinstance(value, BaseModel):
        present = type(value).model_fields
    elif isinstance(value, dict):
        present = value
    else:
        return None
    if SERVICE_ACCOUNT_KEY in present or "service_account" in present:
        return SERVICE_ACCOUNT_KEY
    if SANDBOX_KEY in present:
        return SANDBOX_KEY
    return None
