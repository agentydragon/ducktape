"""Generate the demo workload seeded into the private acceptance repository."""

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    MemoryResources,
    PodSecurityContextProps,
    Secret,
    Service,
    ServicePort,
)

from cluster.cdk8s import pod_policy
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.probes import http_probe

OUTPUT_DIR = f"{GENERATED_ROOT}/gitlab-spike/demo-seed"
_NAMESPACE = "gitlab-spike-demo"
_NAME = "acceptance"


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    deployment = Deployment(
        chart,
        "deployment",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        replicas=1,
        automount_service_account_token=False,
        security_context=PodSecurityContextProps(ensure_non_root=True, user=65532, group=65532),
        docker_registry_auth=Secret.from_secret_name(chart, "pull-credentials", "gitlab-spike-registry"),
    )
    deployment.add_container(
        name="app",
        image="registry-gitlab-spike.allegedly.works/spike/acceptance:unset",
        port_number=8080,
        readiness=http_probe("/healthz", port=8080, initial_delay_seconds=1),
        liveness=http_probe("/healthz", port=8080, initial_delay_seconds=5),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(25)),
            memory=MemoryResources(request=Size.mebibytes(32), limit=Size.mebibytes(64)),
        ),
    )
    pod_policy.harden(deployment)
    Service(
        chart,
        "service",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        selector=deployment,
        ports=[ServicePort(port=8080, target_port=8080)],
    )
    return chart


def write_manifests(root: Path) -> None:
    manifest = write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[manifest], components=["image-pins"]),
    )
