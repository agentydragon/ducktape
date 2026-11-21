"""Crossplane OpenTofu Workspaces for the active Forgejo Terraform roots."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import opentofu, terraform
from cluster.cdk8s.manifest_roots import GENERATED_ROOT


def _kustomization_name(module_name: str) -> str:
    return f"crossplane-{module_name}"


HAKU_STATE_NAME = _kustomization_name("haku-state")
BUDGET_LEDGER_NAME = _kustomization_name("budget-ledger")
CPAP_DATA_NAME = _kustomization_name("cpap-data")
FINANCE_AGENT_NAME = _kustomization_name("finance-agent")
AGENTYDRAGON_REPOS_NAME = _kustomization_name("forgejo-agentydragon-repos")


def output_dir(module_name: str) -> str:
    # The forgejo-gitops ArtifactGenerator copies this parent recursively; child
    # Kustomizations consume their own paths from the shared artifact.
    return f"{GENERATED_ROOT}/forgejo/gitops/{_kustomization_name(module_name)}"


HAKU_STATE_IDENTITY = "opentofu-haku-state"
BUDGET_LEDGER_IDENTITY = "opentofu-budget-ledger"
CPAP_DATA_IDENTITY = "opentofu-cpap-data"
FINANCE_AGENT_IDENTITY = "opentofu-finance-agent"
AGENTYDRAGON_REPOS_IDENTITY = "opentofu-forgejo-agentydragon-repos"


def haku_state_chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name="haku-state",
        identity_name=HAKU_STATE_IDENTITY,
        schema=terraform.gitops_state_schema("haku-state"),
        secret_accesses=(
            (
                "haku-state-forgejo-secrets",
                "forgejo",
                ("forgejo-admin-password",),
                (
                    "haku-forgejo-git",
                    "forgejo-token-mint-haku",
                    "haku-forgejo-registry-pull",
                    "haku-ci-runner-token",
                    "forgejo-webhook-token",
                ),
            ),
        ),
    )


def budget_ledger_chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name="budget-ledger",
        identity_name=BUDGET_LEDGER_IDENTITY,
        schema=terraform.gitops_state_schema("budget-ledger"),
        secret_accesses=(
            ("budget-ledger-forgejo-secrets", "forgejo", ("forgejo-admin-password",), ("budget-ledger-git-creds",)),
        ),
    )


def cpap_data_chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name="cpap-data",
        identity_name=CPAP_DATA_IDENTITY,
        schema=terraform.gitops_state_schema("cpap-data"),
        secret_accesses=(
            (
                "cpap-data-forgejo-secrets",
                "forgejo",
                ("forgejo-admin-password",),
                ("cpap-data-git-write", "cpap-data-git-read"),
            ),
        ),
    )


def finance_agent_chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name="finance-agent",
        identity_name=FINANCE_AGENT_IDENTITY,
        schema=terraform.gitops_state_schema("finance-agent"),
        secret_accesses=(
            (
                "finance-agent-forgejo-secrets",
                "forgejo",
                ("forgejo-admin-password",),
                ("finance-agent-git-creds", "finance-agent-flux-read-credentials", "finance-agent-flux-webhook-token"),
            ),
        ),
    )


def agentydragon_repos_chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name="forgejo-agentydragon-repos",
        identity_name=AGENTYDRAGON_REPOS_IDENTITY,
        # This root's state predates its current module name. Keep its historical schema.
        schema="forgejo_codex",
        secret_accesses=(("forgejo-agentydragon-repos-forgejo-secrets", "forgejo", ("forgejo-admin-password",), ()),),
    )
