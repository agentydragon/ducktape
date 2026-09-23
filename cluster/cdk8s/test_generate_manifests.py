"""Pinning tests for the cdk8s manifest generators.

`test_generated_manifests_match_committed` is the "LiteLLM config pattern"
generated-output snapshot (STYLE.md § Testing), generalized from the ConfigMap
payload to every generated file: the committed files are the source of truth,
and this test proves regeneration reproduces them exactly.
"""

from pathlib import Path

import pytest_bazel

from cluster.cdk8s.generate_manifests import generate_manifests
from util.bazel.runfiles import get_required_path

_GENERATED_FILES = (
    "cluster/k8s/flux/kustomizations.k8s.yaml",
    "cluster/k8s/artifact-generators/artifact-generators.k8s.yaml",
    "cluster/k8s/artifact-generators/kustomization.yaml",
    "cluster/k8s/litellm/app/litellm.k8s.yaml",
    "cluster/k8s/litellm/app/kustomization.yaml",
    "cluster/k8s/agents/ha-mcp/app/ha-mcp.k8s.yaml",
    "cluster/k8s/agents/ha-mcp/app/kustomization.yaml",
    "cluster/k8s/ssh-mcp/ssh-mcp.k8s.yaml",
    "cluster/k8s/ssh-mcp/kustomization.yaml",
    "cluster/k8s/agents/public-coder-agent/sshpiper/pipe-devbox.k8s.yaml",
    "cluster/k8s/agents/public-coder-agent/devbox/public-coder-devbox.k8s.yaml",
    "cluster/k8s/agents/public-coder-agent/devbox/kustomization.yaml",
    "cluster/k8s/agents/public-coder-agent/namespace/public-coder-agent.k8s.yaml",
    "cluster/k8s/agents/public-coder-agent/namespace/kustomization.yaml",
    "cluster/k8s/clickhouse/schema/clickhouse-schema.k8s.yaml",
    "cluster/k8s/clickhouse/schema/kustomization.yaml",
    "cluster/k8s/aiquota/aiquota.k8s.yaml",
    "cluster/k8s/aiquota/kustomization.yaml",
    "cluster/k8s/agentplane-testing/agentplane.k8s.yaml",
    "cluster/k8s/agentplane-testing/litellm-credentials.k8s.yaml",
    "cluster/k8s/agentplane-staging/agentplane.k8s.yaml",
    "cluster/k8s/agentplane-staging/kustomization.yaml",
    "cluster/k8s/haku/console/haku-console.k8s.yaml",
    "cluster/k8s/haku/console/kustomization.yaml",
    "cluster/k8s/agents/haku-openclaw-spike/app/haku-openclaw-spike-config.k8s.yaml",
    "cluster/k8s/agents/haku-openclaw-spike/app/haku-openclaw-spike.k8s.yaml",
    "cluster/k8s/agents/public-coder-agent/app/public-coder-agent-config.k8s.yaml",
    "cluster/k8s/descheduler/helmrelease.k8s.yaml",
    "cluster/k8s/descheduler/rbac.k8s.yaml",
    "cluster/k8s/descheduler/kustomization.yaml",
    "cluster/k8s/seaweedfs/cluster/priorityclass.k8s.yaml",
    "cluster/k8s/external-creds/external-creds.k8s.yaml",
    "cluster/k8s/external-creds/kustomization.yaml",
    "cluster/k8s/agents/haku-egress-proxy/cnp-haku-cloud-api-egress.k8s.yaml",
    "cluster/k8s/agents/haku-egress-proxy/cnp-haku-claude-egress.k8s.yaml",
    "cluster/k8s/agents/haku-egress-proxy/openclaw-spike-cnp-egress.k8s.yaml",
    "cluster/k8s/agents/mitmproxy/cnp-cloud-api-egress.k8s.yaml",
    "cluster/k8s/agents/mitmproxy/kustomization.yaml",
    "cluster/k8s/agents/mitmproxy/mitmproxy.k8s.yaml",
    "cluster/k8s/dns-automation/dns-records.k8s.yaml",
    "cluster/k8s/litellm/keys-tf/litellm-keys.k8s.yaml",
    "cluster/k8s/monitoring/etcd/etcd-monitoring.k8s.yaml",
    "cluster/k8s/monitoring/etcd/kustomization.yaml",
    "cluster/k8s/flux-image-automation-forgejo/flux-image-automation-forgejo.k8s.yaml",
    "cluster/k8s/flux-image-automation-forgejo/kustomization.yaml",
    "cluster/k8s/ntfy/ntfy.k8s.yaml",
    "cluster/k8s/ntfy/kustomization.yaml",
    "cluster/k8s/agents/plaid-mcp/namespace.k8s.yaml",
    "cluster/k8s/tofu-state/namespace.k8s.yaml",
    "cluster/k8s/tofu-state/kustomization.yaml",
    "cluster/k8s/tofu-state/db/tofu-state-db-ovh.k8s.yaml",
    "cluster/k8s/tofu-state/db/kustomization.yaml",
    "cluster/k8s/agents/haku-egress-proxy/namespace.k8s.yaml",
    "cluster/k8s/authentik/db/authentik-db-ovh.k8s.yaml",
    "cluster/k8s/authentik/db/kustomization.yaml",
    "cluster/k8s/authentik/namespace.k8s.yaml",
    "cluster/k8s/clickhouse/operator/namespace.k8s.yaml",
    "cluster/k8s/agents/mitmproxy/namespace.k8s.yaml",
    "cluster/k8s/litellm/namespace.k8s.yaml",
    "cluster/k8s/forgejo/namespace.k8s.yaml",
    "cluster/k8s/forgejo/db/forgejo-db-ssd.k8s.yaml",
    "cluster/k8s/forgejo/db/kustomization.yaml",
    "cluster/k8s/agents/haku-openclaw-spike/app/namespace.k8s.yaml",
    "cluster/k8s/home-assistant/namespace.k8s.yaml",
    "cluster/k8s/github-branch-protection/github-branch-protection.k8s.yaml",
    "cluster/k8s/github-branch-protection/kustomization.yaml",
    "cluster/k8s/agents/machine-access-tf/agent-machine-access.k8s.yaml",
    "cluster/k8s/agents/machine-access-tf/kustomization.yaml",
    "cluster/k8s/forgejo/agentydragon/forgejo-agentydragon.k8s.yaml",
    "cluster/k8s/forgejo/agentydragon/kustomization.yaml",
    "cluster/k8s/forgejo/agentydragon-repos/forgejo-agentydragon-repos.k8s.yaml",
    "cluster/k8s/forgejo/agentydragon-repos/kustomization.yaml",
    "cluster/k8s/forgejo/budget-ledger/budget-ledger.k8s.yaml",
    "cluster/k8s/forgejo/budget-ledger/kustomization.yaml",
    "cluster/k8s/forgejo/claude/forgejo-claude.k8s.yaml",
    "cluster/k8s/forgejo/claude/kustomization.yaml",
    "cluster/k8s/forgejo/cpap-data/cpap-data.k8s.yaml",
    "cluster/k8s/forgejo/cpap-data/kustomization.yaml",
    "cluster/k8s/forgejo/haku-state/haku-state.k8s.yaml",
    "cluster/k8s/forgejo/haku-state/kustomization.yaml",
    "cluster/k8s/forgejo-images/forgejo-images.k8s.yaml",
    "cluster/k8s/forgejo-images/kustomization.yaml",
    "cluster/k8s/monitoring/alloy-otlp-bearer-token-tf/alloy-otlp-bearer-token.k8s.yaml",
    "cluster/k8s/monitoring/alloy-otlp-bearer-token-tf/kustomization.yaml",
    "cluster/k8s/infra-drift/infra-drift.k8s.yaml",
    "cluster/k8s/github-secrets-sync/github-secrets-sync.k8s.yaml",
    "cluster/k8s/github-secrets-sync/kustomization.yaml",
    "cluster/k8s/gatus/sso-tf/gatus-sso.k8s.yaml",
    "cluster/k8s/gatus/sso-tf/kustomization.yaml",
    "cluster/k8s/flux-webhook-token/flux-webhook-token.k8s.yaml",
    "cluster/k8s/flux-webhook-token/kustomization.yaml",
    "cluster/k8s/authentik/sso-providers-tf/sso-providers.k8s.yaml",
    "cluster/k8s/authentik/sso-providers-tf/kustomization.yaml",
)


def test_generated_manifests_match_committed(tmp_path: Path) -> None:
    """Regenerate with `bb run //cluster/cdk8s:generate_manifests` and commit the result if this fails."""
    generate_manifests(tmp_path)
    for relative in _GENERATED_FILES:
        generated = (tmp_path / relative).read_text()
        committed = get_required_path(f"ducktape/{relative}").read_text()
        assert generated == committed, f"{relative} is stale"
        # cdk8s can't emit YAML comments, so this should be unreachable -- but if it
        # ever did, Flux's image-automation bot would silently fight the generator
        # for ownership of this file (cluster/docs/cdk8s.md).
        assert "$imagepolicy" not in generated, f"{relative} must not carry a Flux image-automation marker"


if __name__ == "__main__":
    pytest_bazel.main()
