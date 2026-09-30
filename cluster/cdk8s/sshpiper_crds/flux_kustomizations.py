"""The sshpiper `Pipe` CRD, applied straight from upstream `plugin/kubernetes/crd.yaml` in the
`sshpiper-source` GitRepository (`flux_sources.py`). The cdk8s `Pipe` binding reads the same
file through the SHA-256-pinned `sshpiper_pipe_crd` repository in `MODULE.bazel`; the CRD is
never copied into this repository.

The CRD has its own Kustomization, not its consumer's (today only `public_coder_sshpiper`), so
that it outlives any one sshpiperd deployment.
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
        description="The sshpiper Pipe CRD, sourced from the tag used by the deployed image.",
    )
