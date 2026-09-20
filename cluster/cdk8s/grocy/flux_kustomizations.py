"""Flux Kustomizations for the cluster/k8s/grocy slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def grocy_sf(
    chart: Chart,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    cert_manager_issuer_config: Kustomization,
    cert_manager_environment: Kustomization,
    authentik: Kustomization,
    volsync: Kustomization,
) -> Kustomization:
    name = "grocy-sf"
    return flux_kustomization(
        chart,
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
            depends_on=flux_kustomization_depends_on_many(
                forgejo_images, gateway, cert_manager_issuer_config, cert_manager_environment, authentik, volsync
            ),
        ),
    )


def grocy_mcp_sf(
    chart: Chart,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    grocy_sf: Kustomization,
    valkey: Kustomization,
    agent_machine_access_tf: Kustomization,
    reflector: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "grocy-mcp-sf"
    return flux_kustomization(
        chart,
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
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                forgejo_images,
                gateway,
                grocy_sf,
                valkey,
                agent_machine_access_tf,
                reflector,
                # the ServiceMonitor/PodMonitor CRD
                monitoring_crds,
            ),
        ),
    )


def grocy_sf_user_perms(chart: Chart, forgejo_images: Kustomization, grocy_sf: Kustomization) -> Kustomization:
    name = "grocy-sf-user-perms"
    return flux_kustomization(
        chart,
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
            depends_on=flux_kustomization_depends_on_many(forgejo_images, grocy_sf),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="batch/v1", kind="Job", name="grocy-user-perms-provisioner", namespace="grocy-sf"
                )
            ],
        ),
    )


def grocy_vallejo(
    chart: Chart,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    cert_manager_issuer_config: Kustomization,
    cert_manager_environment: Kustomization,
    authentik: Kustomization,
    volsync: Kustomization,
) -> Kustomization:
    name = "grocy-vallejo"
    return flux_kustomization(
        chart,
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
            depends_on=flux_kustomization_depends_on_many(
                forgejo_images, gateway, cert_manager_issuer_config, cert_manager_environment, authentik, volsync
            ),
        ),
    )


def grocy_mcp_vallejo(
    chart: Chart,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    grocy_vallejo: Kustomization,
    valkey: Kustomization,
    agent_machine_access_tf: Kustomization,
    reflector: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "grocy-mcp-vallejo"
    return flux_kustomization(
        chart,
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
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                forgejo_images,
                gateway,
                grocy_vallejo,
                valkey,
                agent_machine_access_tf,
                reflector,
                # the ServiceMonitor/PodMonitor CRD
                monitoring_crds,
            ),
        ),
    )


def grocy_vallejo_user_perms(
    chart: Chart, forgejo_images: Kustomization, grocy_vallejo: Kustomization
) -> Kustomization:
    name = "grocy-vallejo-user-perms"
    return flux_kustomization(
        chart,
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
            depends_on=flux_kustomization_depends_on_many(forgejo_images, grocy_vallejo),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="batch/v1", kind="Job", name="grocy-user-perms-provisioner", namespace="grocy-vallejo"
                )
            ],
        ),
    )
