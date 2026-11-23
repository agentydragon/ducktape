"""Deployment order for the GitLab evaluation instance and its proof integrations."""

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from pydantic import BaseModel
from python.runfiles import Runfiles

from cluster.cdk8s import opentofu
from cluster.cdk8s.cnpg.health import database_health
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

GITOPS_NAME = "gitlab-spike-gitops"
GITOPS_DIR = f"{GENERATED_ROOT}/gitlab-spike/gitops"
KUBERNETES_IDENTITY = "opentofu-gitlab-spike"
INTEGRATIONS_NAME = "gitlab-spike-integrations"
INTEGRATIONS_DIR = f"{GENERATED_ROOT}/gitlab-spike/integrations"


class SshKey(BaseModel):
    key: str


class Variables(BaseModel):
    human_ssh_keys: dict[str, SshKey]


def gitops_chart(app: App) -> Chart:
    chart = Chart(app, GITOPS_NAME, disable_resource_name_hashes=True)
    runfiles_root = Runfiles.CreateOrRaise().root()
    variables = Variables(
        human_ssh_keys={
            host: SshKey(key=(runfiles_root / f"ducktape/ssh_keys/{host}-forgejo.pub").read_text().strip())
            for host in ("atlas", "iguana", "rugged", "wyrm2")
        }
    )
    identity = opentofu.kubernetes_service_account(chart, KUBERNETES_IDENTITY)
    for access_id, namespace, reads, writes in (
        (
            "gitlab-spike-secrets",
            "gitlab-spike",
            ("gitlab-bootstrap-token", "gitlab-spike-oidc"),
            ("gitlab-spike-proof", "gitlab-spike-benchmark-token", "forgejo-spike-benchmark"),
        ),
        ("forgejo-secrets", "forgejo", ("forgejo-admin-password",), ()),
        ("flux-secrets", "flux-system", (), ("gitlab-spike-git", "gitlab-spike-registry", "gitlab-spike-webhook")),
        ("demo-secrets", "gitlab-spike-demo", (), ("gitlab-spike-registry",)),
        ("runner-secrets", "gitlab-spike-ci", (), ("gitlab-spike-runner",)),
    ):
        opentofu.secret_access(chart, access_id, identity=identity, namespace=namespace, read=reads, write=writes)
    opentofu.gitops_workspace(
        chart,
        "workspace",
        name="gitlab-spike",
        provider_config_name="crossplane-gitlab-spike",
        management_policies=["Observe", "Create", "Update"],
        kubernetes_identity=KUBERNETES_IDENTITY,
        variables=variables,
    )
    return chart


def dependencies(
    chart: Chart,
    directory: RenderedDirectory,
    external_secrets: Kustomization,
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
        depends_on=flux_kustomization_depends_on_many(external_secrets, cnpg, seaweedfs, valkey, piraeus),
        health_check_exprs=[database_health()],
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


def gitops(
    chart: Chart,
    directory: RenderedDirectory,
    bootstrap: Kustomization,
    tofu_controller: Kustomization,
    forgejo: Kustomization,
    crossplane_opentofu: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        GITOPS_NAME,
        directory,
        timeout="15m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(bootstrap, tofu_controller, forgejo, crossplane_opentofu),
    )
