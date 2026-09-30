"""The tf/gitops modules that manage Forgejo users, repositories and grants, as one Flux unit:
one tofu-controller `Terraform` CR per module, ordered among themselves by each CR's own
`spec.dependsOn`."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "forgejo-gitops"
OUTPUT_DIR = f"{GENERATED_ROOT}/forgejo/gitops"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    claude = terraform.gitops_terraform(chart, "claude", name="forgejo-claude", variables=None)
    # forgejo_collaborator.claude grants read to the claude agent account, so the
    # forgejo-claude module (which provisions that user) must apply first.
    haku_state = terraform.gitops_terraform(chart, "haku-state", name="haku-state", variables=None, depends_on=[claude])
    # Collaborator grants reference service users provisioned by these modules.
    terraform.gitops_terraform(
        chart, "budget-ledger", name="budget-ledger", variables=None, depends_on=[claude, haku_state]
    )
    terraform.gitops_terraform(chart, "cpap-data", name="cpap-data", variables=None, depends_on=[claude, haku_state])
    terraform.gitops_terraform(
        chart,
        "agentydragon-repos",
        name="forgejo-agentydragon-repos",
        variables=None,
        # Haku source-read collaborator grants look up the haku user provisioned by
        # tf/gitops/haku-state.
        depends_on=[haku_state],
        # Historical state schema from the previous module name.
        # Rename only with an explicit tofu-state migration.
        schema="forgejo_codex",
    )
    terraform.gitops_terraform(chart, "agentydragon", name="forgejo-agentydragon", variables=None)
    # forgejo_collaborator.haku grants write to the haku agent account provisioned by
    # tf/gitops/haku-state.
    terraform.gitops_terraform(chart, "finance-agent", name="finance-agent", variables=None, depends_on=[haku_state])
    return chart


def forgejo_gitops(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, NAME, directory, timeout="10m", depends_on=flux_kustomization_depends_on_many(tofu_controller)
    )
