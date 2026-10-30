"""Crossplane Inline Workspace for GitHub Actions secrets and variables."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import opentofu, terraform
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.nix_cache import ci_token

NAME = "github-secrets-sync"
WORKSPACE_NAME = f"crossplane-{NAME}"
OUTPUT_DIR = f"{GENERATED_ROOT}/github-tf/{WORKSPACE_NAME}"
KUBERNETES_IDENTITY = f"opentofu-{NAME}"


def chart(app: App) -> Chart:
    chart = Chart(app, WORKSPACE_NAME, disable_resource_name_hashes=True)
    identity = opentofu.kubernetes_service_account(chart, KUBERNETES_IDENTITY)
    opentofu.secret_access(
        chart,
        "flux-system-secrets",
        identity=identity,
        namespace="flux-system",
        read=[
            # keep-sorted start
            "buildbuddy-api-key",
            "ci-age-key",
            "ducktape-automation-github-app",
            "github-secrets-sync-pat",
            "pr-visuals-s3-credentials",
            # keep-sorted end
        ],
    )
    opentofu.secret_access(
        chart, "forgejo-images-secrets", identity=identity, namespace="forgejo-images", read=["forgejo-images-creds"]
    )
    opentofu.secret_access(
        chart,
        "attic-ci-token",
        identity=identity,
        namespace=ci_token.WRITER.namespace,
        read=[ci_token.READER.name, ci_token.WRITER.name],
    )
    opentofu.gitops_workspace(
        chart,
        "workspace",
        name=NAME,
        provider_config_name=WORKSPACE_NAME,
        schema=terraform.gitops_state_schema(NAME),
        directory=f"tf/gitops/{NAME}",
        management_policies=["Observe", "Create", "Update"],
        kubernetes_identity=KUBERNETES_IDENTITY,
    )
    return chart
