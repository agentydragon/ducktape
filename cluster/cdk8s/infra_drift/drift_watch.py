"""Drift detection for the metal root (cluster/terraform/main). That root is applied from a
workstation by `bazel run //cluster:bootstrap`; this CR only ever plans it, so the operator
learns about reality/state divergence without the controller being able to act on it. See
cluster/cdk8s/infra_drift/README.md."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_gitrepository_crds.io.fluxcd.toolkit.source import GitRepositorySpecRef
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGenerator,
    ArtifactGeneratorSpec,
    ArtifactGeneratorSpecArtifacts,
    ArtifactGeneratorSpecArtifactsCopy,
    ArtifactGeneratorSpecSources,
    ArtifactGeneratorSpecSourcesKind,
)
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformV1Alpha2SpecSourceRef,
    TerraformV1Alpha2SpecSourceRefKind,
    TerraformV1Alpha2SpecStoreReadablePlan,
)

from cluster.cdk8s import ducktape_flux, terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.flux.git_repository import GitRepository
from cluster.cdk8s.secret_ref import SecretRef
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAME = "infra-drift"
OUTPUT_DIR = f"{GENERATED_ROOT}/infra-drift"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # Dedicated source: the shared flux-system GitRepository sparse-checks-out only
    # deployment paths (cluster/k8s/, tf/gitops/, ...), so cluster/terraform/ is absent from
    # its artifact and the runner fails with "terraform path not found". Its sparseCheckout
    # cannot be extended either: gotk-sync.yaml is flux-generated and marked DO NOT EDIT.
    source = GitRepository(
        chart,
        "source",
        metadata=ApiObjectMetadata(
            name="infra-drift-source",
            namespace=terraform.NAMESPACE,
            annotations={
                "description": "Filtered checkout of the metal tofu root, its module tree, the SOPS secrets it reads "
                "and nebula-mesh.json, for the infra-drift plan-only Terraform CR."
            },
        ),
        interval="10m",
        ref=GitRepositorySpecRef(branch=ducktape_flux.BRANCH),
        # This source clones the whole repository history like any other ducktape source, so it
        # races the same 60s default: it failed `unable to clone ... context deadline exceeded` on
        # 2026-10-15. Its `ignore` pattern below does not help, because `ignore` filters the
        # checkout after the fetch, not the transfer. Reasoning in ducktape_flux.CLONE_TIMEOUT.
        timeout=ducktape_flux.CLONE_TIMEOUT,
        url=ducktape_flux.REPOSITORY_URL,
        # `ignore` rather than `sparseCheckout`: nebula.tf's locals read the repo-root
        # nebula-mesh.json, and sparseCheckout takes directories only. Exclude-all then
        # re-include, spelling out each parent: gitignore cannot re-include a path whose
        # parent directory is excluded.
        ignore=(get_required_path(own_repo_rlocation("cluster/cdk8s/infra_drift/source.gitignore")).read_text()),
    )
    # The GitRepository's revision is the commit, so every push to the branch would
    # replan. The ExternalArtifact's is a digest of the filtered tree: a plan runs when
    # a file the root reads changes, or on the interval.
    module = ArtifactGenerator(
        chart,
        "artifact",
        metadata=ApiObjectMetadata(name=NAME, namespace=terraform.NAMESPACE),
        spec=ArtifactGeneratorSpec(
            sources=[
                ArtifactGeneratorSpecSources(
                    alias="repo",
                    kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY,
                    name=source.name,
                    namespace=terraform.NAMESPACE,
                )
            ],
            artifacts=[
                ArtifactGeneratorSpecArtifacts(
                    name=NAME,
                    origin_revision="@repo",
                    copy=[ArtifactGeneratorSpecArtifactsCopy(from_="@repo/**", to="@artifact/")],
                )
            ],
        ),
    )
    terraform.tofu_state_terraform(
        chart,
        "terraform",
        name=NAME,
        annotations={
            "description": (
                "Plan-only drift watch on cluster/terraform/main (the metal root). Never applies;"
                " `bazel run //cluster:bootstrap` remains the apply path."
            )
        },
        plan_only=True,
        # Without this the pending plan says only that something changed. `human`
        # writes the diff to a ConfigMap in this namespace — README § Reading a plan.
        store_readable_plan=TerraformV1Alpha2SpecStoreReadablePlan.HUMAN,
        # Every plan takes the PG advisory lock on schema `main` — the lock
        # `bazel run //cluster:bootstrap` also needs. README § State lock contention
        # before shortening this.
        interval="6h",
        path="./cluster/terraform/main",
        source_ref=TerraformV1Alpha2SpecSourceRef(
            kind=TerraformV1Alpha2SpecSourceRefKind.EXTERNAL_ARTIFACT, name=module.name, namespace=terraform.NAMESPACE
        ),
        # The OVH server resources and nothing else — the one subgraph whose only
        # dependencies are the ovh provider and two SOPS files. README § What this
        # covers has the reason for each exclusion, Proxmox included.
        targets=[
            "ovh_dedicated_server.kimsufi",
            # Zero instances while local.kimsufi_cp_servers is empty; listed so a
            # future Kimsufi control plane is watched without editing this CR.
            "ovh_dedicated_server.kimsufi_cp",
        ],
        schema="main",
        env=[
            # Decrypts secrets/ovh-{credentials,rescue-ssh}.sops.yaml so the ovh
            # provider can configure. This is the broad cluster age key, not a
            # narrow one: tf-runner-role is a ClusterRole granting secret CRUD in
            # every namespace, so any runner can already read this very Secret — a
            # single-purpose key would be ceremony, not containment.
            terraform.secret_env(
                "SOPS_AGE_KEY",
                SecretRef(namespace=terraform.NAMESPACE, name="sops-age-cluster-secrets").key("age.agekey"),
            )
        ],
    )
    return chart


def infra_drift(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        wait=None,
        timeout="10m",
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
        depends_on=flux_kustomization_depends_on_many(tofu_controller),
    )
