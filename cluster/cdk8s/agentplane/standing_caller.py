"""A standing workload's reviewed Identity and its component ServiceAccount reference."""

from __future__ import annotations

from dataclasses import dataclass

from agentplane_policybinding_crds.works.allegedly.agentplane import (
    PolicyBindingSpecSubject,
    PolicyBindingSpecSubjectIdentity,
)

from agentplane.subjects import ServiceAccountRef


@dataclass(frozen=True)
class StandingCaller:
    service_account: ServiceAccountRef
    # The `Identity`'s name, in the policy namespace.
    identity: str

    def identity_subject(self) -> PolicyBindingSpecSubject:
        return PolicyBindingSpecSubject(identity=PolicyBindingSpecSubjectIdentity(name=self.identity))
