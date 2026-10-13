"""The subject an Agentplane policy binds to, defined once for every service that binds one.

A subject is a Kubernetes ServiceAccount: the one a workload's Pod runs as, which is what both the
egress proxy and the Action Service authenticate it by, and what the Kubernetes API server
authorizes. `PolicyBinding` carries this model's JSON schema
as its subject, spliced in by `bb run //agentplane/crds:generate_bin`.

Every sandbox runs as a ServiceAccount of its own, so a binding names no Sandbox and follows no Pod
owner, and an agent hosted outside this cluster is a subject on the same terms as one inside it.
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
