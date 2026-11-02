"""The sshpiper `Pipe` CRD, applied straight from upstream `plugin/kubernetes/crd.yaml` in the
`sshpiper-source` GitRepository (`flux_sources.py`). The CRD is not copied into this repository.

The CRD has its own non-pruning Kustomization so it outlives individual sshpiperd deployments.
Keep it until a cluster-wide inventory confirms no other Pipe resources depend on it.
"""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def sshpiper_crds(chart: Chart) -> Kustomization:
    name = "sshpiper-crds"
    return flux_kustomization(
        chart,
        name,
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="sshpiper-source", namespace="ducktape-flux"
        ),
        timeout="2m",
        path="./plugin/kubernetes",
        # Pruning a CRD deletes every instance with it; removing one is a deliberate manual step, as
        # for the other CRD Kustomizations (agentplane-crds, external-secrets-crds).
        prune=False,
        description="The upstream sshpiper Pipe CRD; pruning is disabled to protect any remaining Pipe resources.",
    )
