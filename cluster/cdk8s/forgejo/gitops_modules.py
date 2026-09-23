"""The tf/gitops modules that manage Forgejo users, repositories and grants: one
tofu-controller `Terraform` CR per `cluster/k8s/forgejo/<dir>`, and each directory's
Flux node."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from tofu_controller.io.fluxcd.contrib.infra import TerraformV1Alpha2

from cluster.cdk8s import terraform
from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_yaml

CLAUDE = "forgejo-claude"
HAKU_STATE = "haku-state"
BUDGET_LEDGER = "budget-ledger"
CPAP_DATA = "cpap-data"
AGENTYDRAGON_REPOS = "forgejo-agentydragon-repos"
AGENTYDRAGON = "forgejo-agentydragon"
CLAUDE_DIR = "cluster/k8s/forgejo/claude"
HAKU_STATE_DIR = "cluster/k8s/forgejo/haku-state"
BUDGET_LEDGER_DIR = "cluster/k8s/forgejo/budget-ledger"
CPAP_DATA_DIR = "cluster/k8s/forgejo/cpap-data"
AGENTYDRAGON_REPOS_DIR = "cluster/k8s/forgejo/agentydragon-repos"
AGENTYDRAGON_DIR = "cluster/k8s/forgejo/agentydragon"


def _write(
    root: Path, directory: str, name: str, *, depends_on: Sequence[TerraformV1Alpha2] = (), schema: str | None = None
) -> TerraformV1Alpha2:
    out_dir = root / directory
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    module = terraform.gitops_terraform(
        Chart(app, name, disable_resource_name_hashes=True),
        "terraform",
        name=name,
        variables={},
        depends_on=[dependency.name for dependency in depends_on],
        schema=schema,
    )
    app.synth()
    write_yaml(out_dir / "kustomization.yaml", kustomize_kustomization(resources=[f"{name}.k8s.yaml"]))
    return module


def write_manifests(root: Path) -> None:
    claude = _write(root, CLAUDE_DIR, CLAUDE)
    # forgejo_collaborator.claude grants read to the claude agent account, so the
    # forgejo-claude module (which provisions that user) must apply first.
    haku_state = _write(root, HAKU_STATE_DIR, HAKU_STATE, depends_on=[claude])
    # Collaborator grants reference service users provisioned by these modules.
    _write(root, BUDGET_LEDGER_DIR, BUDGET_LEDGER, depends_on=[claude, haku_state])
    _write(root, CPAP_DATA_DIR, CPAP_DATA, depends_on=[claude, haku_state])
    _write(
        root,
        AGENTYDRAGON_REPOS_DIR,
        AGENTYDRAGON_REPOS,
        # Haku source-read collaborator grants look up the haku user provisioned by
        # tf/gitops/haku-state.
        depends_on=[haku_state],
        # Historical state schema from the previous module name.
        # Rename only with an explicit tofu-state migration.
        schema="forgejo_codex",
    )
    _write(root, AGENTYDRAGON_DIR, AGENTYDRAGON)


def forgejo_claude(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    claude_rbac: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        CLAUDE,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the claude Forgejo service user + the
            # claude-forgejo-credentials Secret) so repo read-grants (e.g. gaffer-private
            # tf/thrive-scrape) and agent sessions can depend on it.
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name=CLAUDE,
                    namespace=terraform.NAMESPACE,
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # the credentials Secret lands in claude-sandbox
                claude_rbac,
            ),
        ),
    )


def haku_state(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    agentplane_index: Kustomization,
    haku_namespace: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        HAKU_STATE,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the Forgejo repo + service user + the
            # haku-forgejo-git Secret) so scan runs can depend on it.
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name=HAKU_STATE,
                    namespace=terraform.NAMESPACE,
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # The git-creds Secret is reflected into agentplane-index; wait for the
                # aggregate to create that target Namespace before applying Terraform.
                agentplane_index,
                haku_namespace,
            ),
        ),
    )


def budget_ledger(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    budget_namespace: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        BUDGET_LEDGER,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the Forgejo repo + service user + the
            # budget-ledger-git-creds Secret) so the exporter/Fava can depend on it.
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name=BUDGET_LEDGER,
                    namespace=terraform.NAMESPACE,
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # the git-creds Secret lands in the budget namespace
                budget_namespace,
            ),
        ),
    )


def cpap_data(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    cpap_sync: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        CPAP_DATA,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the Forgejo repo + service users + the
            # cpap-data-git-{write,read} Secrets) so the sync CronJob can depend on it.
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name=CPAP_DATA,
                    namespace=terraform.NAMESPACE,
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # the git-creds Secrets land in the cpap-sync namespace
                cpap_sync,
            ),
        ),
    )


def forgejo_agentydragon_repos(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        AGENTYDRAGON_REPOS,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            # Wait for the Terraform apply (adopts ducktape/gaffer-private, creates
            # collaborator service users/keys, and grants repo access).
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name=AGENTYDRAGON_REPOS,
                    namespace=terraform.NAMESPACE,
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
            ),
        ),
    )


def forgejo_agentydragon(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, tofu_controller: Kustomization, tofu_state_db: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        AGENTYDRAGON,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name=AGENTYDRAGON,
                    namespace=terraform.NAMESPACE,
                )
            ],
            depends_on=flux_kustomization_depends_on_many(tofu_controller, tofu_state_db),
        ),
    )
