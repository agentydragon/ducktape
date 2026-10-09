"""The namespace shared by Agentplane environments."""

from __future__ import annotations

from constructs import Construct

from cluster.cdk8s import namespaces
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.namespaces import Vpa


class Namespace(Construct):
    """The namespace shared by an Agentplane environment."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self,
            "namespace",
            name=env.namespace,
            # Runner Pods are Sandbox-owned, not Deployments; nothing here is VPA-managed.
            vpa=Vpa.DISABLED,
            # Standing agent access to metadata and logs (Kyverno-generated bindings).
            # Environment-specific write access is layered on by its chart.
            labels={"name": env.namespace},
            annotations={"description": env.description},
        )
