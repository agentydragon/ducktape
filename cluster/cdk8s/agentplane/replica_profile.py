"""The replicas/strategy/topology-spread/minReadySeconds shape shared by every
Agentplane staging/testing Deployment (llm-ingress, egress, app): staging runs 2
replicas with a zero-unavailable rolling update, topology spread across nodes, and a
minReadySeconds drain buffer; testing runs a single replica with Recreate and neither.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s import Duration
from cdk8s_plus_34 import DeploymentStrategy, PercentOrAbsolute


@dataclass(frozen=True)
class ReplicaProfile:
    replicas: int
    strategy: DeploymentStrategy
    topology_spread: bool
    min_ready: Duration | None


STAGING = ReplicaProfile(
    replicas=2,
    strategy=DeploymentStrategy.rolling_update(
        max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
    ),
    topology_spread=True,
    min_ready=Duration.seconds(5),
)
TESTING = ReplicaProfile(replicas=1, strategy=DeploymentStrategy.recreate(), topology_spread=False, min_ready=None)
