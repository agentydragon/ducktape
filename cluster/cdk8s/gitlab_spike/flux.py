"""Deployment order for the GitLab evaluation instance and its proof integrations."""

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
)
from pydantic import BaseModel
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import CNPG_DATABASE_READY
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from util.bazel.runfiles import get_required_path, own_repo_rlocation

GITOPS_NAME = "gitlab-spike-gitops"
GITOPS_DIR = f"{GENERATED_ROOT}/gitlab-spike/gitops"


class SshKey(BaseModel):
    key: str


class Variables(BaseModel):
    human_ssh_keys: dict[str, SshKey]


def gitops_chart(app: App, *, module: ArtifactGeneratorSpecArtifacts) -> Chart:
    chart = Chart(app, GITOPS_NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(
        chart,
        "terraform",
        module=module,
        variables=Variables(
            human_ssh_keys={
                host: SshKey(
                    key=get_required_path(own_repo_rlocation(f"ssh_keys/{host}-forgejo.pub")).read_text().strip()
                )
                for host in ("atlas", "iguana", "rugged", "wyrm2")
            }
        ),
    )
    return chart


def namespace(chart: Chart, directory: RenderedDirectory, external_secrets: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        "gitlab-spike-namespace",
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(external_secrets),
    )


def dependencies(
    chart: Chart,
    directory: RenderedDirectory,
    namespace: Kustomization,
    cnpg: Kustomization,
    seaweedfs: Kustomization,
    valkey: Kustomization,
    piraeus: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "gitlab-spike-dependencies",
        directory,
        timeout="20m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(namespace, cnpg, seaweedfs, valkey, piraeus),
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
            )
        ],
    )


def app(
    chart: Chart, directory: RenderedDirectory, dependencies: Kustomization, authentik: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "gitlab-spike",
        directory,
        timeout="30m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(dependencies, authentik),
    )


def bootstrap(chart: Chart, directory: RenderedDirectory, app: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, "gitlab-spike-bootstrap", directory, timeout="15m", depends_on=flux_kustomization_depends_on_many(app)
    )


def gitops(
    chart: Chart,
    directory: RenderedDirectory,
    bootstrap: Kustomization,
    tofu_controller: Kustomization,
    forgejo: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        GITOPS_NAME,
        directory,
        timeout="15m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(bootstrap, tofu_controller, forgejo),
    )


def runner(chart: Chart, directory: RenderedDirectory, gitops: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, "gitlab-spike-runner", directory, timeout="10m", depends_on=flux_kustomization_depends_on_many(gitops)
    )


def delivery(chart: Chart, directory: RenderedDirectory, gitops: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        "gitlab-spike-delivery",
        directory,
        # The initial demo commit and first CI image are submitted by the proof harness.
        wait=False,
        depends_on=flux_kustomization_depends_on_many(gitops),
    )


def benchmark(chart: Chart, directory: RenderedDirectory, gitops: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, "gitlab-spike-benchmark", directory, depends_on=flux_kustomization_depends_on_many(gitops)
    )
