"""The tf/gitops modules that manage Forgejo users, repositories and grants, as one Flux unit:
one tofu-controller `Terraform` CR per module, ordered among themselves by each CR's own
`spec.dependsOn`."""

from __future__ import annotations

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "forgejo-gitops"
OUTPUT_DIR = f"{GENERATED_ROOT}/forgejo/gitops"


def chart(
    app: App,
    *,
    claude: ArtifactGeneratorSpecArtifacts,
    haku_state: ArtifactGeneratorSpecArtifacts,
    budget_ledger: ArtifactGeneratorSpecArtifacts,
    cpap_data: ArtifactGeneratorSpecArtifacts,
    agentydragon_repos: ArtifactGeneratorSpecArtifacts,
    agentydragon: ArtifactGeneratorSpecArtifacts,
    finance_agent: ArtifactGeneratorSpecArtifacts,
) -> Chart:
    """One CR per module, each reading the artifact its keyword names (`terraform.gitops_module`)."""
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    claude_tf = terraform.gitops_terraform(chart, "claude", module=claude, variables=None)
    # forgejo_collaborator.claude grants read to the claude agent account, so the
    # forgejo-claude module (which provisions that user) must apply first.
    haku_state_tf = terraform.gitops_terraform(
        chart, "haku-state", module=haku_state, variables=None, depends_on=[claude_tf]
    )
    # Collaborator grants reference service users provisioned by these modules.
    terraform.gitops_terraform(
        chart, "budget-ledger", module=budget_ledger, variables=None, depends_on=[claude_tf, haku_state_tf]
    )
    terraform.gitops_terraform(
        chart, "cpap-data", module=cpap_data, variables=None, depends_on=[claude_tf, haku_state_tf]
    )
    terraform.gitops_terraform(
        chart,
        "agentydragon-repos",
        module=agentydragon_repos,
        variables=None,
        # Haku source-read collaborator grants look up the haku user provisioned by
        # tf/gitops/haku-state.
        depends_on=[haku_state_tf],
        # Historical state schema from the previous module name.
        # Rename only with an explicit tofu-state migration.
        schema="forgejo_codex",
    )
    terraform.gitops_terraform(chart, "agentydragon", module=agentydragon, variables=None)
    # forgejo_collaborator.haku grants write to the haku agent account provisioned by
    # tf/gitops/haku-state.
    terraform.gitops_terraform(chart, "finance-agent", module=finance_agent, variables=None, depends_on=[haku_state_tf])
    return chart


def forgejo_gitops(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, NAME, directory, timeout="10m", depends_on=flux_kustomization_depends_on_many(tofu_controller)
    )
