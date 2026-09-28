"""haku-forgejo-tea: the SOPS-encrypted Forgejo API token Reflector mirrors into haku-ci's KEDA
scaler. The Secret stays hand-written; this builds the directory's Flux Kustomization."""

from __future__ import annotations

from cdk8s import Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "haku-forgejo-tea"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/haku/forgejo-tea"


def haku_forgejo_tea(flux_chart: Chart, directory: RenderedDirectory, haku_rbac: Kustomization) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=[
            # haku-sandbox ns the secret lives in
            flux_kustomization_depends_on(haku_rbac)
        ],
        description=(
            "Forgejo API token Reflector mirrors into haku-ci's KEDA scaler. "
            "Split out of haku/managed-agent so haku-ci doesn't depend on the "
            "(parked) worker."
        ),
    )
