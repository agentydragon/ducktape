"""Drift detection for the metal root (cluster/terraform/main). That root is applied from a
workstation by `bazel run //cluster:bootstrap`; this CR only ever plans it, so the operator
learns about reality/state divergence without the controller being able to act on it. See
cluster/k8s/infra-drift/README.md."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_gitrepository_crds.io.fluxcd.toolkit.source import GitRepository, GitRepositorySpec, GitRepositorySpecRef
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformV1Alpha2,
    TerraformV1Alpha2Spec,
    TerraformV1Alpha2SpecBackendConfig,
    TerraformV1Alpha2SpecRunnerPodTemplate,
    TerraformV1Alpha2SpecRunnerPodTemplateSpec,
    TerraformV1Alpha2SpecSourceRef,
    TerraformV1Alpha2SpecSourceRefKind,
    TerraformV1Alpha2SpecStoreReadablePlan,
)

from cluster.cdk8s import terraform
from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "infra-drift"
OUTPUT_DIR = "cluster/k8s/infra-drift"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # Dedicated source: the shared flux-system GitRepository sparse-checks-out only
    # deployment paths (cluster/k8s/, tf/gitops/, ...), so cluster/terraform/ is absent from
    # its artifact and the runner fails with "terraform path not found". Its sparseCheckout
    # cannot be extended either: gotk-sync.yaml is flux-generated and marked DO NOT EDIT.
    source = GitRepository(
        chart,
        "source",
        metadata=metadata(
            "infra-drift-source",
            terraform.NAMESPACE,
            annotations={
                "description": "Filtered checkout of the metal tofu root, its module tree, the SOPS secrets it reads "
                "and nebula-mesh.json, for the infra-drift plan-only Terraform CR."
            },
        ),
        spec=GitRepositorySpec(
            interval="10m",
            ref=GitRepositorySpecRef(branch="devel"),
            url="https://github.com/agentydragon/ducktape.git",
            # `ignore` rather than `sparseCheckout`: nebula.tf's locals read the repo-root
            # nebula-mesh.json, and sparseCheckout takes directories only. Exclude-all then
            # re-include, spelling out each parent: gitignore cannot re-include a path whose
            # parent directory is excluded.
            ignore=(
                "/*\n"
                "!/nebula-mesh.json\n"
                "# data.sops_file.ovh_{credentials,rescue_ssh} and, if -target ever stops\n"
                "# pruning them, the nebula certs under secrets/nebula/. Ciphertext only.\n"
                "!/secrets\n"
                "!/cluster\n"
                "/cluster/*\n"
                "!/cluster/terraform\n"
                "# Every file()-family call in the root, wherever it sits: -target does not\n"
                "# prune configuration evaluation. talos-cloud-controller-manager for\n"
                "# talos-ccm.tf's locals, flux-system for null_resource.flux_bootstrap's\n"
                "# triggers. The rest of cluster/k8s (10 MB) reads nothing.\n"
                "!/cluster/k8s\n"
                "/cluster/k8s/*\n"
                "!/cluster/k8s/talos-cloud-controller-manager\n"
                "!/cluster/k8s/flux\n"
                "/cluster/k8s/flux/*\n"
                "!/cluster/k8s/flux/flux-system\n"
                '# module "wyrm2_image": `tofu init` installs every module block in the\n'
                "# config, whether or not -target keeps it in the plan graph.\n"
                "!/terraform\n"
                "/terraform/*\n"
                "!/terraform/modules\n"
            ),
        ),
    )
    TerraformV1Alpha2(
        chart,
        "terraform",
        metadata=metadata(
            NAME,
            terraform.NAMESPACE,
            annotations={
                "description": (
                    "Plan-only drift watch on cluster/terraform/main (the metal root). Never applies;"
                    " `bazel run //cluster:bootstrap` remains the apply path."
                )
            },
        ),
        spec=TerraformV1Alpha2Spec(
            plan_only=True,
            # A plan without refresh compares config to state and would report "no
            # changes" against a reality nobody has looked at — refresh is what makes
            # drift observable (cluster/docs/lessons_learned/2026_04_25_tofu_controller_refresh_before_apply.md).
            refresh_before_apply=True,
            # Without this the pending plan says only that something changed. `human`
            # writes the diff to a ConfigMap in this namespace — README § Reading a plan.
            store_readable_plan=TerraformV1Alpha2SpecStoreReadablePlan.HUMAN,
            # Every plan takes the PG advisory lock on schema `main` — the lock
            # `bazel run //cluster:bootstrap` also needs. README § State lock contention
            # before shortening this.
            interval="6h",
            path="./cluster/terraform/main",
            source_ref=TerraformV1Alpha2SpecSourceRef(
                kind=TerraformV1Alpha2SpecSourceRefKind.GIT_REPOSITORY, name=source.name, namespace=terraform.NAMESPACE
            ),
            service_account_name="tf-runner",
            # The OVH server resources and nothing else — the one subgraph whose only
            # dependencies are the ovh provider and two SOPS files. README § What this
            # covers has the reason for each exclusion, Proxmox included.
            targets=[
                "ovh_dedicated_server.kimsufi",
                # Zero instances while local.kimsufi_cp_servers is empty; listed so a
                # future Kimsufi control plane is watched without editing this CR.
                "ovh_dedicated_server.kimsufi_cp",
            ],
            backend_config=TerraformV1Alpha2SpecBackendConfig(
                custom_configuration=(
                    f'backend "pg" {{\n  conn_str    = "{terraform.STATE_DB}"\n  schema_name = "main"\n}}\n'
                )
            ),
            runner_pod_template=TerraformV1Alpha2SpecRunnerPodTemplate(
                spec=TerraformV1Alpha2SpecRunnerPodTemplateSpec(
                    env=[
                        terraform.secret_env("PGPASSWORD", "tofu-state-db-credentials", "password"),
                        # Decrypts secrets/ovh-{credentials,rescue-ssh}.sops.yaml so the ovh
                        # provider can configure. This is the broad cluster age key, not a
                        # narrow one: tf-runner-role is a ClusterRole granting secret CRUD in
                        # every namespace, so any runner can already read this very Secret — a
                        # single-purpose key would be ceremony, not containment.
                        terraform.secret_env("SOPS_AGE_KEY", "sops-age-cluster-secrets", "age.agekey"),
                    ]
                )
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def infra_drift(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, tofu_controller: Kustomization, tofu_state_db: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            # Ready tracks the plan, so a drift finding shows up here as a NotReady
            # Kustomization — README § Reading a plan.
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name=NAME,
                    namespace=terraform.NAMESPACE,
                )
            ],
            depends_on=flux_kustomization_depends_on_many(tofu_controller, tofu_state_db),
        ),
    )
