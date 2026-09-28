"""Ducktape's policy for agent-sandbox objects, built on `providers/agent_sandbox`."""

from __future__ import annotations

from agent_sandbox_sandboxwarmpool_crds.io.x_k8s.agents.extensions import (
    SandboxWarmPoolSpecSandboxTemplateRef,
    SandboxWarmPoolSpecUpdateStrategy,
    SandboxWarmPoolSpecUpdateStrategyType,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.providers.agent_sandbox.sandbox_template import SandboxTemplate
from cluster.cdk8s.providers.agent_sandbox.sandbox_warm_pool import SandboxWarmPool


def warm_pool(scope: Construct, id: str, *, template: SandboxTemplate) -> SandboxWarmPool:
    """One idle sandbox built from `template`, in a pool that takes the template's name and, since
    the reference is by name alone, its namespace. `Recreate`, not the CRD's `OnReplenish`: a
    changed pod template replaces the idle sandbox at once instead of leaving it for the next claim.
    """
    return SandboxWarmPool(
        scope,
        id,
        metadata=ApiObjectMetadata(name=template.name, namespace=template.metadata.namespace),
        sandbox_template_ref=SandboxWarmPoolSpecSandboxTemplateRef(name=template.name),
        replicas=1,
        update_strategy=SandboxWarmPoolSpecUpdateStrategy(type=SandboxWarmPoolSpecUpdateStrategyType.RECREATE),
    )
