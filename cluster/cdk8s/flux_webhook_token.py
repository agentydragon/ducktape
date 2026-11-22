"""Crossplane Inline Workspace for Flux's GitHub webhook token."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import opentofu
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "flux-webhook-token"
WORKSPACE_NAME = f"crossplane-{NAME}"
OUTPUT_DIR = f"{GENERATED_ROOT}/github-tf/{WORKSPACE_NAME}"
KUBERNETES_IDENTITY = f"opentofu-{NAME}"


def chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name=NAME,
        identity_name=KUBERNETES_IDENTITY,
        secret_accesses=(
            ("flux-system-secrets", "flux-system", ("github-secrets-sync-pat",), ("github-webhook-token",)),
        ),
    )
