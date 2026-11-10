"""A Flux-rendered `PolicyBinding` subject, by name only.

Today a standing caller is the ServiceAccount it authenticates as. In the identity catalog
(`Environment.identity_catalog`) its bindings name an `Identity` of its own instead, which the staging
chart renders; the ServiceAccount stays the caller's credential until its services authenticate by
identity. Haku's standing workloads and the haku-ci relay are not standing callers: their SVIDs
already act as Identities the chart renders unconditionally (haku/ui_egress.py,
haku_ci/egress_gateway.py).
"""

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

    def subject(self, *, identity_catalog: bool) -> PolicyBindingSpecSubject:
        if identity_catalog:
            return PolicyBindingSpecSubject(identity=PolicyBindingSpecSubjectIdentity(name=self.identity))
        return PolicyBindingSpecSubject(namespace=self.service_account.namespace, name=self.service_account.name)
