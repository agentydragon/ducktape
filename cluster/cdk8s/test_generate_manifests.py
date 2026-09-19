"""Pinning tests for the cdk8s manifest generators.

`test_generated_manifests_match_committed` is the "LiteLLM config pattern"
generated-output snapshot (STYLE.md § Testing), generalized from the ConfigMap
payload to every converted directory's whole generated file set: the committed
files are the source of truth, and this test proves regeneration reproduces
them exactly.
"""

from pathlib import Path

import pytest_bazel

from cluster.cdk8s.generate_manifests import generate_manifests
from util.bazel.runfiles import get_required_path

_GENERATED_FILES = (
    "cluster/k8s/artifact-generators/artifact-generators.k8s.yaml",
    "cluster/k8s/artifact-generators/flux-kustomization.yaml",
    "cluster/k8s/artifact-generators/kustomization.yaml",
    "cluster/k8s/litellm/app/litellm.k8s.yaml",
    "cluster/k8s/litellm/app/flux-kustomization.yaml",
    "cluster/k8s/litellm/app/kustomization.yaml",
    "cluster/k8s/agents/ha-mcp/app/ha-mcp.k8s.yaml",
    "cluster/k8s/agents/ha-mcp/app/flux-kustomization.yaml",
    "cluster/k8s/agents/ha-mcp/app/kustomization.yaml",
    "cluster/k8s/ssh-mcp/ssh-mcp.k8s.yaml",
    "cluster/k8s/ssh-mcp/flux-kustomization.yaml",
    "cluster/k8s/ssh-mcp/kustomization.yaml",
    "cluster/k8s/agents/public-coder-agent/sshpiper/pipe-devbox.k8s.yaml",
    "cluster/k8s/agents/public-coder-agent/devbox/public-coder-devbox.k8s.yaml",
    "cluster/k8s/agents/public-coder-agent/devbox/kustomization.yaml",
    "cluster/k8s/agents/public-coder-agent/namespace/public-coder-agent.k8s.yaml",
    "cluster/k8s/agents/public-coder-agent/namespace/kustomization.yaml",
    "cluster/k8s/clickhouse/schema/clickhouse-schema.k8s.yaml",
    "cluster/k8s/clickhouse/schema/flux-kustomization.yaml",
    "cluster/k8s/clickhouse/schema/kustomization.yaml",
    "cluster/k8s/aiquota/aiquota.k8s.yaml",
    "cluster/k8s/aiquota/flux-kustomization.yaml",
    "cluster/k8s/aiquota/kustomization.yaml",
    "cluster/k8s/agentplane-testing/agentplane.k8s.yaml",
    "cluster/k8s/agentplane-testing/flux-kustomization.yaml",
    "cluster/k8s/agentplane-testing/kustomization.yaml",
    "cluster/k8s/agentplane-testing/litellm-credentials/litellm-credentials.k8s.yaml",
    "cluster/k8s/agentplane-testing/litellm-credentials/kustomization.yaml",
    "cluster/k8s/agentplane-staging/agentplane.k8s.yaml",
    "cluster/k8s/agentplane-staging/flux-kustomization.yaml",
    "cluster/k8s/agentplane-staging/kustomization.yaml",
    "cluster/k8s/haku/console/haku-console.k8s.yaml",
    "cluster/k8s/haku/console/flux-kustomization.yaml",
    "cluster/k8s/haku/console/kustomization.yaml",
    "cluster/k8s/agents/haku-openclaw-spike/app/haku-openclaw-spike-config.k8s.yaml",
    "cluster/k8s/agents/public-coder-agent/app/public-coder-agent-config.k8s.yaml",
    "cluster/k8s/descheduler/helmrelease.k8s.yaml",
    "cluster/k8s/seaweedfs/cluster/priorityclass.k8s.yaml",
    "cluster/k8s/external-creds/external-creds.k8s.yaml",
    "cluster/k8s/external-creds/flux-kustomization.yaml",
    "cluster/k8s/external-creds/kustomization.yaml",
    "cluster/k8s/agents/haku-egress-proxy/cnp-haku-cloud-api-egress.k8s.yaml",
    "cluster/k8s/agents/haku-egress-proxy/cnp-haku-claude-egress.k8s.yaml",
    "cluster/k8s/agents/haku-egress-proxy/openclaw-spike-cnp-egress.k8s.yaml",
    "cluster/k8s/agents/mitmproxy/cnp-cloud-api-egress.k8s.yaml",
    "cluster/k8s/dns-automation/dns-records.k8s.yaml",
    "cluster/k8s/litellm/keys-tf/litellm-keys.k8s.yaml",
    "cluster/k8s/monitoring/etcd/etcd-monitoring.k8s.yaml",
    "cluster/k8s/monitoring/etcd/flux-kustomization.yaml",
    "cluster/k8s/monitoring/etcd/kustomization.yaml",
    "cluster/k8s/flux-image-automation-forgejo/flux-image-automation-forgejo.k8s.yaml",
    "cluster/k8s/flux-image-automation-forgejo/flux-kustomization.yaml",
    "cluster/k8s/flux-image-automation-forgejo/kustomization.yaml",
    "cluster/k8s/ntfy/ntfy.k8s.yaml",
    "cluster/k8s/ntfy/flux-kustomization.yaml",
    "cluster/k8s/ntfy/kustomization.yaml",
    "cluster/k8s/parked/agent-box/flux-kustomization.yaml",
    "cluster/k8s/parked/archivebox/flux-kustomization.yaml",
    "cluster/k8s/parked/augur-evidence/flux-kustomization.yaml",
    "cluster/k8s/parked/authelia/flux-kustomization.yaml",
    "cluster/k8s/parked/browsertrix/app/flux-kustomization.yaml",
    "cluster/k8s/parked/browsertrix/bucket/flux-kustomization.yaml",
    "cluster/k8s/parked/browsertrix/namespace/flux-kustomization.yaml",
    "cluster/k8s/parked/browsertrix/retained/flux-kustomization.yaml",
    "cluster/k8s/parked/budget/flux-kustomization.yaml",
    "cluster/k8s/parked/buildbuddy-executor/flux-kustomization.yaml",
    "cluster/k8s/parked/cloud-agent-tf/flux-kustomization.yaml",
    "cluster/k8s/parked/docker-ci/flux-kustomization.yaml",
    "cluster/k8s/parked/egress-proxy-rugged/flux-kustomization.yaml",
    "cluster/k8s/parked/firecrawl/app/flux-kustomization.yaml",
    "cluster/k8s/parked/firecrawl/db/flux-kustomization.yaml",
    "cluster/k8s/parked/firecrawl/namespace/flux-kustomization.yaml",
    "cluster/k8s/parked/gecko/app/flux-kustomization.yaml",
    "cluster/k8s/parked/gecko/namespace/flux-kustomization.yaml",
    "cluster/k8s/parked/google-workspace-mcp/flux-kustomization.yaml",
    "cluster/k8s/parked/haku-dispatch/flux-kustomization.yaml",
    "cluster/k8s/parked/inventree/app/flux-kustomization.yaml",
    "cluster/k8s/parked/inventree/db/flux-kustomization.yaml",
    "cluster/k8s/parked/inventree/namespace/flux-kustomization.yaml",
    "cluster/k8s/parked/inventree/token-provisioner/flux-kustomization.yaml",
    "cluster/k8s/parked/kubectl-machine-mcp/flux-kustomization.yaml",
    "cluster/k8s/parked/managed-agent/flux-kustomization.yaml",
    "cluster/k8s/parked/manifold-mcp/flux-kustomization.yaml",
    "cluster/k8s/parked/openhands/app/flux-kustomization.yaml",
    "cluster/k8s/parked/openhands/namespace/flux-kustomization.yaml",
    "cluster/k8s/parked/openhands/sandboxes/flux-kustomization.yaml",
    "cluster/k8s/parked/osm-mcp/flux-kustomization.yaml",
    "cluster/k8s/parked/paperless/app/flux-kustomization.yaml",
    "cluster/k8s/parked/paperless/cache/flux-kustomization.yaml",
    "cluster/k8s/parked/paperless/db/flux-kustomization.yaml",
    "cluster/k8s/parked/paperless/namespace/flux-kustomization.yaml",
    "cluster/k8s/parked/postscanmail-mcp/flux-kustomization.yaml",
    "cluster/k8s/parked/sdr/flux-kustomization.yaml",
    "cluster/k8s/parked/tandoor/app/flux-kustomization.yaml",
    "cluster/k8s/parked/tandoor/db/flux-kustomization.yaml",
    "cluster/k8s/parked/tandoor/namespace/flux-kustomization.yaml",
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
