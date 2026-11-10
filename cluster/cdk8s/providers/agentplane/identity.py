"""Ergonomic wrapper for Agentplane's own `Identity` CRD
(agentplane/crds/manifests/crd-identities.yaml), following cdk8s-plus's own construction pattern: a
class named after the kind.
"""

from __future__ import annotations

from agentplane_identity_crds.works.allegedly.agentplane import Identity as _Identity, IdentitySpec
from agentplane_policybinding_crds.works.allegedly.agentplane import (
    PolicyBindingSpecSubject,
    PolicyBindingSpecSubjectIdentity,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class Identity(_Identity):
    """Agentplane's `Identity`: who an agent is, a policy subject and nothing else."""

    def __init__(self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, description: str) -> None:
        super().__init__(scope, id, metadata=metadata, spec=IdentitySpec(description=description))

    def binding_subject(self) -> PolicyBindingSpecSubject:
        """This Identity as a `PolicyBinding`'s subject, which the binding's namespace must be."""
        return PolicyBindingSpecSubject(identity=PolicyBindingSpecSubjectIdentity(name=self.name))
