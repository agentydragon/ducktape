"""The subject an Agentplane policy binds to, defined once for every service that binds one.

A subject is a Kubernetes ServiceAccount: the one a workload's Pod runs as, which is what both the
egress proxy and the Action Service authenticate it by. `EgressBinding` and `ActionPolicyBinding`
carry this model's JSON schema as their subject, spliced in by `bb run //agentplane/crds:generate_bin`.

A Sandbox was a subject too, resolved by following a Pod to the Sandbox that owns it. That reach
only ever existed for workloads this cluster provisions, so an agent hosted anywhere else could not
be a subject at all. Every sandbox now runs as a ServiceAccount of its own, which leaves one kind of
subject and no owner to follow. A Sandbox is still an identity -- `llm_ingress` attributes model
calls to one -- but nothing authorizes against it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ServiceAccountRef(BaseModel):
    """One ServiceAccount, by namespace and name.

    Namespaced because a subject is not always in the namespace enforcing it: an agent this cluster
    does not host runs where it runs, and naming its namespace is how a binding reaches it.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
