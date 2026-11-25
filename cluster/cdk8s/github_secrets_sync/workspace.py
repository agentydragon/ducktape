"""Crossplane Inline Workspace for GitHub Actions secrets and variables."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import opentofu
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.nix_cache import ci_token

NAME = "github-secrets-sync"
WORKSPACE_NAME = f"crossplane-{NAME}"
OUTPUT_DIR = f"{GENERATED_ROOT}/github-tf/{WORKSPACE_NAME}"
KUBERNETES_IDENTITY = f"opentofu-{NAME}"


def chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name=NAME,
        identity_name=KUBERNETES_IDENTITY,
        secret_accesses=(
            (
                "flux-system-secrets",
                "flux-system",
                (
                    # keep-sorted start
                    "buildbuddy-api-key",
                    "ci-age-key",
                    "ducktape-automation-github-app",
                    "gaffer-repo-fetch-pat",
                    "github-secrets-sync-pat",
                    "pr-visuals-s3-credentials",
                    # keep-sorted end
                ),
                (),
            ),
            ("forgejo-images-secrets", "forgejo-images", ("forgejo-images-creds",), ()),
            (
                "attic-ci-token",
                ci_token.WRITER.namespace,
                (ci_token.GAFFER_WRITER.name, ci_token.READER.name, ci_token.WRITER.name),
                (),
            ),
            ("drivefs-artifacts-s3-credentials", "seaweedfs-credentials", ("drivefs-artifacts-s3-credentials",), ()),
        ),
    )
