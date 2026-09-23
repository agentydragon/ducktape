"""The tf/gitops modules that manage Forgejo users, repositories and grants: one
tofu-controller `Terraform` CR per `cluster/k8s/forgejo/<dir>`, and each directory's
Flux node."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from tofu_controller.io.fluxcd.contrib.infra import TerraformV1Alpha2

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many

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
        # Wait for the Terraform apply (creates the claude Forgejo service user + the
        # claude-forgejo-credentials Secret) so repo read-grants (e.g. gaffer-private
        # tf/thrive-scrape) and agent sessions can depend on it.
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            # Forgejo API must be up (provider target)
            forgejo,
            tofu_controller,
            tofu_state_db,
            # the credentials Secret lands in claude-sandbox
            claude_rbac,
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
        # Wait for the Terraform apply (creates the Forgejo repo + service user + the
        # haku-forgejo-git Secret) so scan runs can depend on it.
        artifact,
        timeout="10m",
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
        # Wait for the Terraform apply (creates the Forgejo repo + service user + the
        # budget-ledger-git-creds Secret) so the exporter/Fava can depend on it.
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            # Forgejo API must be up (provider target)
            forgejo,
            tofu_controller,
            tofu_state_db,
            # the git-creds Secret lands in the budget namespace
            budget_namespace,
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
        # Wait for the Terraform apply (creates the Forgejo repo + service users + the
        # cpap-data-git-{write,read} Secrets) so the sync CronJob can depend on it.
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            # Forgejo API must be up (provider target)
            forgejo,
            tofu_controller,
            tofu_state_db,
            # the git-creds Secrets land in the cpap-sync namespace
            cpap_sync,
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
        # Wait for the Terraform apply (adopts ducktape/gaffer-private, creates
        # collaborator service users/keys, and grants repo access).
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            # Forgejo API must be up (provider target)
            forgejo,
            tofu_controller,
            tofu_state_db,
        ),
    )


def forgejo_agentydragon(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, tofu_controller: Kustomization, tofu_state_db: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        AGENTYDRAGON,
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(tofu_controller, tofu_state_db),
    )
