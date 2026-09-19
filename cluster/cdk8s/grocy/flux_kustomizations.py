"""Flux Kustomizations for the cluster/k8s/grocy slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def grocy_sf() -> dict[str, object]:
    name = "grocy-sf"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/grocy/sf/app",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-issuer-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="volsync", namespace="ducktape-flux"),
            ],
        ),
    )


def grocy_mcp_sf() -> dict[str, object]:
    name = "grocy-mcp-sf"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/grocy/sf/mcp",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="grocy-mcp-server", namespace="grocy-sf"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grocy-sf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the ServiceMonitor/PodMonitor CRD
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def grocy_sf_user_perms() -> dict[str, object]:
    name = "grocy-sf-user-perms"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/grocy/sf/user-perms",
            prune=True,
            # Run only after grocy-sf is up (and self-migrated via its postStart hook); the
            # Job-completion healthcheck makes this kustomization Ready only once the policy
            # in policy.yaml has actually been applied — so a fresh cluster converges to the
            # committed user→permission policy.
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grocy-sf", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="batch/v1", kind="Job", name="grocy-user-perms-provisioner", namespace="grocy-sf"
                )
            ],
        ),
    )


def grocy_vallejo() -> dict[str, object]:
    name = "grocy-vallejo"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/grocy/vallejo/app",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-issuer-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="volsync", namespace="ducktape-flux"),
            ],
        ),
    )


def grocy_mcp_vallejo() -> dict[str, object]:
    name = "grocy-mcp-vallejo"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/grocy/vallejo/mcp",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="grocy-mcp-server", namespace="grocy-vallejo"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grocy-vallejo", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the ServiceMonitor/PodMonitor CRD
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def grocy_vallejo_user_perms() -> dict[str, object]:
    name = "grocy-vallejo-user-perms"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/grocy/vallejo/user-perms",
            prune=True,
            # Run only after grocy-vallejo is up (and self-migrated via its postStart hook);
            # the Job-completion healthcheck makes this kustomization Ready only once the
            # policy in policy.yaml has actually been applied — so a fresh cluster converges
            # to the committed user→permission policy.
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grocy-vallejo", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="batch/v1", kind="Job", name="grocy-user-perms-provisioner", namespace="grocy-vallejo"
                )
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/grocy/sf/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, grocy_sf())
    path = root / "cluster/k8s/grocy/sf/mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, grocy_mcp_sf())
    path = root / "cluster/k8s/grocy/sf/user-perms/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, grocy_sf_user_perms())
    path = root / "cluster/k8s/grocy/vallejo/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, grocy_vallejo())
    path = root / "cluster/k8s/grocy/vallejo/mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, grocy_mcp_vallejo())
    path = root / "cluster/k8s/grocy/vallejo/user-perms/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, grocy_vallejo_user_perms())
