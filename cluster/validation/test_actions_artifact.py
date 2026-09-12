"""Contracts for source-artifact migrations."""

import shutil
import subprocess
from pathlib import Path

import pytest_bazel
import yaml

from cluster.validation.tool_resolve import resolve_tool
from util.bazel.runfiles import get_required_path


def test_artifact_generators_preserve_render_inputs(tmp_path: Path) -> None:
    root = get_required_path("_main/cluster/k8s/kustomization.yaml").parent
    root_kustomization = yaml.safe_load((root / "kustomization.yaml").read_text())
    expected_source = {"alias": "repo", "kind": "GitRepository", "name": "ducktape", "namespace": "ducktape-flux"}
    expected_sources = {
        "external-creds": {"alias": "repo", "kind": "GitRepository", "name": "flux-system", "namespace": "flux-system"}
    }
    kustomize = resolve_tool("kustomize", "multitool/tools/kustomize/kustomize")

    artifact_relative = "artifact-generators"
    generators = {
        document["metadata"]["name"]: document
        for document in yaml.safe_load_all((root / artifact_relative / "generators.yaml").read_text())
        if document and document.get("kind") == "ArtifactGenerator"
    }
    cases = (
        ("agentplane-staging-actions", "agentplane-staging/actions"),
        ("agentplane-staging-app", "agentplane-staging/app"),
        ("agentplane-staging-namespace", "agentplane-staging/namespace"),
        ("agentplane-testing-actions", "agentplane-testing/actions"),
        ("claude-rbac", "agents/agent-rbac-base"),
        ("agent-machine-access-tf", "agents/machine-access-tf"),
        ("authentik", "authentik/app"),
        ("sso-providers-tf", "authentik/sso-providers-tf"),
        ("cert-manager", "cert-manager/app"),
        ("cert-manager-environment", "cert-manager/environment"),
        ("cert-manager-issuer-config", "cert-manager/issuer-config"),
        ("cnpg", "cnpg"),
        ("external-secrets-config", "external-secrets/config"),
        ("external-secrets-operator", "external-secrets/operator"),
        ("forgejo", "forgejo/app"),
        ("forgejo-images", "forgejo-images"),
        ("gateway", "gateway"),
        ("kyverno", "kyverno/app"),
        ("litellm-keys-tf", "litellm/keys-tf"),
        ("local-path-provisioner", "local-path-provisioner"),
        ("monitoring-stack", "monitoring/stack"),
        ("reflector", "reflector"),
        ("seaweedfs-cluster", "seaweedfs/cluster"),
        ("seaweedfs-filer-db", "seaweedfs/db"),
        ("seaweedfs-namespace", "seaweedfs/namespace"),
        ("seaweedfs-operator", "seaweedfs/operator"),
        ("seaweedfs-secrets", "seaweedfs/secrets"),
        ("tofu-controller", "tofu-controller"),
        ("tofu-state-db", "tofu-state/db"),
        ("valkey", "valkey"),
        ("kyverno-policies", "kyverno/policies"),
        ("haku-state", "forgejo/haku-state"),
        ("agentplane-crds", "agentplane-crds"),
        ("monitoring-namespace", "monitoring/namespace"),
        ("haku-namespace", "haku/namespace"),
        ("langfuse-namespace", "langfuse/namespace"),
        ("volsync", "volsync"),
        ("authentik-namespace", "authentik/namespace"),
        ("cert-manager-trust", "cert-manager/trust"),
        ("agent-sandbox-controller", "agents/agent-sandbox/controller"),
        ("external-creds", "external-creds"),
        ("agentplane-testing-namespace", "agentplane-testing/namespace"),
        ("grafana-helmrepository", "monitoring/grafana-helmrepository"),
        ("kubevirt", "kubevirt/app"),
        ("paperless-namespace", "paperless/namespace"),
        ("public-coder-agent-namespace", "agents/public-coder-agent/namespace"),
        ("agentplane-index-namespace", "agentplane-index/namespace"),
        ("agentplane-staging-db", "agentplane-staging/db"),
        ("agentplane-testing-db", "agentplane-testing/db"),
        ("clickhouse", "clickhouse/cluster"),
        ("forgejo-namespace", "forgejo/namespace"),
        ("atuin-namespace", "atuin/namespace"),
        ("budget-namespace", "budget/namespace"),
        ("github-secrets-sync-secrets", "github-secrets-sync/secrets"),
        ("grafana-instance", "monitoring/grafana-instance"),
        ("ha-mcp-namespace", "agents/ha-mcp/namespace"),
        ("haku-egress-proxy", "agents/haku-egress-proxy"),
        ("haku-rbac", "haku/rbac"),
        ("langfuse-secrets", "langfuse/secrets"),
        ("litellm-namespace", "litellm/namespace"),
        ("agentplane-egress-credentials-namespace", "agentplane-egress-credentials/namespace"),
        ("agentplane-egress-credentials", "agentplane-egress-credentials/secrets"),
        ("agentplane-staging-agent-rbac", "agentplane-staging/agent-rbac"),
        ("agentplane-staging-egress", "agentplane-staging/egress"),
        ("agentplane-staging-llm-ingress", "agentplane-staging/llm-ingress"),
        ("agentplane-testing-agent-rbac", "agentplane-testing/agent-rbac"),
        ("agentplane-testing-app", "agentplane-testing/app"),
        ("agentplane-testing-dex", "agentplane-testing/dex"),
        ("agentplane-testing-egress", "agentplane-testing/egress"),
        ("agentplane-testing-llm-ingress", "agentplane-testing/llm-ingress"),
        ("litellm-secrets", "litellm/secrets"),
        ("matrix-namespace", "matrix/namespace"),
        ("matrix-secrets", "matrix/secrets"),
        ("ollama-namespace", "ollama/namespace"),
        ("seaweedfs-csi", "seaweedfs-csi"),
        ("seaweedfs-public-s3", "seaweedfs/public-s3"),
        ("ssh-mcp-secrets", "ssh-mcp/secrets"),
        ("study-casino-namespace", "study-casino/namespace"),
        ("tana-mcp", "agents/tana-mcp"),
        ("authentik-jwt-rotation", "agents/authentik-jwt-rotation"),
        ("inventree-namespace", "inventree/namespace"),
        ("authentik-sso-secrets", "authentik/sso-secrets"),
        ("cdi", "kubevirt/cdi"),
        ("firecrawl-namespace", "firecrawl/namespace"),
        ("flux-image-automation-ghcr", "flux-image-automation-ghcr"),
        ("gatus-namespace", "gatus/namespace"),
        ("grafana-operator", "monitoring/grafana-operator"),
        ("grocy-sf", "grocy/sf/app"),
        ("grocy-vallejo", "grocy/vallejo/app"),
        ("haku-console-namespace", "haku/console-namespace"),
        ("haku-egress-proxy-namespace", "agents/haku-egress-proxy-namespace"),
        ("haku-mailbox-namespace", "haku/mailbox-namespace"),
        ("haku-openclaw-spike-namespace", "agents/haku-openclaw-spike/namespace"),
        ("home-assistant", "home-assistant/app"),
        ("inventree-secrets", "inventree/secrets"),
        ("litellm", "litellm/app"),
        ("monitoring-stack-secrets", "monitoring/stack-secrets"),
        ("nix-cache", "nix-cache/app"),
        ("nvidia-device-plugin", "nvidia-device-plugin"),
        ("ollama-secrets", "ollama/secrets"),
        ("attic-jwt-rotation", "agents/attic-jwt-rotation"),
        ("claude-sandbox-secrets", "agents/claude-sandbox-secrets"),
        ("coinbase-read", "agents/coinbase-read"),
        ("forgejo-token-rotation", "agents/forgejo-token-rotation"),
        ("ha-mcp", "agents/ha-mcp/app"),
        ("ha-mcp-credentials", "agents/ha-mcp/credentials"),
        ("ha-mcp-servicemonitor", "agents/ha-mcp/servicemonitor"),
        ("haku-openclaw-spike-app", "agents/haku-openclaw-spike/app"),
        ("haku-openclaw-spike-backup", "agents/haku-openclaw-spike/backup"),
        ("kubectl-machine-mcp", "agents/kubectl-machine-mcp/app"),
        ("kubectl-machine-mcp-namespace", "agents/kubectl-machine-mcp/namespace"),
        ("kubectl-passthrough-mcp", "agents/kubectl-passthrough-mcp/app"),
        ("kubectl-passthrough-mcp-namespace", "agents/kubectl-passthrough-mcp/namespace"),
        ("loki-read-proxy", "agents/loki-read-proxy"),
        ("manifold-mcp", "agents/manifold-mcp/app"),
        ("manifold-mcp-namespace", "agents/manifold-mcp/namespace"),
        ("manifold-mcp-servicemonitor", "agents/manifold-mcp/servicemonitor"),
        ("agents-mitmproxy-namespace", "agents/mitmproxy-namespace"),
        ("osm-mcp", "agents/osm-mcp/app"),
        ("osm-mcp-namespace", "agents/osm-mcp/namespace"),
        ("plaid-db-mcp", "agents/plaid-db-mcp/app"),
        ("plaid-db-mcp-servicemonitor", "agents/plaid-db-mcp/servicemonitor"),
        ("plaid-mcp", "agents/plaid-mcp/app"),
        ("plaid-mcp-db", "agents/plaid-mcp/db"),
        ("plaid-mcp-namespace", "agents/plaid-mcp/namespace"),
        ("postscanmail-mcp", "agents/postscanmail-mcp/app"),
        ("postscanmail-mcp-namespace", "agents/postscanmail-mcp/namespace"),
        ("postscanmail-mcp-servicemonitor", "agents/postscanmail-mcp/servicemonitor"),
        ("public-coder-agent-app", "agents/public-coder-agent/app"),
        ("public-coder-agent-backup", "agents/public-coder-agent/backup"),
        ("public-coder-agent-devbox", "agents/public-coder-agent/devbox"),
        ("public-coder-agent-k8s-reader", "agents/public-coder-agent/k8s-reader"),
        ("public-coder-agent-proxy", "agents/public-coder-agent/proxy"),
        ("public-coder-agent-sshpiper", "agents/public-coder-agent/sshpiper"),
        ("agent-shared-rbac", "agents/shared-rbac"),
        ("agent-shared-secrets", "agents/shared-secrets"),
        ("tana-mcp-facade", "agents/tana-mcp-facade"),
        ("tana-mcp-facade-monitoring", "agents/tana-mcp-facade/monitoring"),
        ("aiquota", "aiquota"),
        ("clickhouse-grafana", "analytics/grafana"),
        ("atuin", "atuin/app"),
        ("atuin-db", "atuin/db"),
        ("atuin-secrets", "atuin/secrets"),
        ("atuin-user-provisioner", "atuin/user-provisioner"),
        ("authentik-db", "authentik/db"),
        ("authentik-proxy-routes", "authentik/proxy-routes"),
        ("authentik-secrets", "authentik/secrets"),
        ("authentik-servicemonitor", "authentik/servicemonitor"),
        ("budget-app", "budget/app"),
        ("cert-manager-servicemonitor", "cert-manager/servicemonitor"),
        ("cli-proxy-api", "cli-proxy-api"),
        ("clickhouse-namespace", "clickhouse/namespace"),
        ("clickhouse-operator", "clickhouse/operator"),
        ("clickhouse-schema", "clickhouse/schema"),
        ("coredns-custom", "coredns-custom"),
        ("cpap-sync", "cpap-sync"),
        ("dcgm-exporter", "dcgm-exporter"),
        ("descheduler", "descheduler"),
        ("dns-automation", "dns-automation"),
        ("docker-ci", "docker-ci"),
        ("egress-proxy-rugged", "egress-proxy-rugged"),
        ("evidence-market-roster", "evidence/market-roster"),
        ("evidence-namespace", "evidence/namespace"),
        ("firecrawl", "firecrawl/app"),
        ("firecrawl-db", "firecrawl/db"),
        ("flux-grafana-secrets", "flux-grafana-secrets"),
        ("flux-image-automation-forgejo", "flux-image-automation-forgejo"),
        ("flux-monitoring", "flux-monitoring"),
        ("flux-webhook", "flux-webhook"),
        ("flux-webhook-token", "flux-webhook-token"),
        ("forgejo-agentydragon", "forgejo/agentydragon"),
        ("forgejo-agentydragon-repos", "forgejo/agentydragon-repos"),
        ("augur-evidence", "forgejo/augur-evidence"),
        ("budget-ledger", "forgejo/budget-ledger"),
        ("forgejo-cache", "forgejo/cache"),
        ("forgejo-claude", "forgejo/claude"),
        ("cpap-data", "forgejo/cpap-data"),
        ("forgejo-db", "forgejo/db"),
        ("forgejo-secrets", "forgejo/secrets"),
        ("forgejo-servicemonitor", "forgejo/servicemonitor"),
        ("gatus", "gatus/app"),
        ("gatus-db", "gatus/db"),
        ("gatus-servicemonitor", "gatus/servicemonitor"),
        ("gatus-sso-tf", "gatus/sso-tf"),
        ("gecko", "gecko/app"),
        ("gecko-namespace", "gecko/namespace"),
        ("github-api-proxy", "github-api-proxy/app"),
        ("github-api-proxy-identity", "github-api-proxy/identity"),
        ("github-api-proxy-secrets", "github-api-proxy/secrets"),
        ("github-branch-protection", "github-branch-protection"),
        ("github-exporter", "github-exporter"),
        ("github-secrets-sync", "github-secrets-sync"),
        ("goldilocks", "goldilocks"),
        ("grocy-mcp-sf", "grocy/sf/mcp"),
        ("grocy-mcp-sf-servicemonitor", "grocy/sf/mcp-servicemonitor"),
        ("grocy-sf-user-perms", "grocy/sf/user-perms"),
        ("grocy-mcp-vallejo", "grocy/vallejo/mcp"),
        ("grocy-mcp-vallejo-servicemonitor", "grocy/vallejo/mcp-servicemonitor"),
        ("grocy-vallejo-user-perms", "grocy/vallejo/user-perms"),
        ("haku-cloud-agent", "haku/cloud-agent-tf"),
        ("haku-console-db", "haku/console/db"),
        ("haku-console", "haku/console"),
        ("haku-console-migration", "haku/console/migration"),
        ("haku-console-servicemonitor", "haku/console/servicemonitor"),
        ("haku-mailbox-app", "haku/mailbox/app"),
        ("haku-mailbox-db", "haku/mailbox/db"),
        ("haku-managed-agent", "haku/managed-agent"),
        ("haku-ui-image-webhook", "haku/ui-image-webhook"),
        ("haku-workloads", "haku/workloads"),
        ("haku-workspaces-app", "haku/workspaces/app"),
        ("haku-ci", "haku-ci"),
        ("headlamp-app", "headlamp/app"),
        ("headlamp-namespace", "headlamp/namespace"),
        ("home-assistant-namespace", "home-assistant/namespace"),
        ("hubble-ui", "hubble-ui"),
        ("infra-drift", "infra-drift"),
        ("inventree-app", "inventree/app"),
        ("inventree-db", "inventree/db"),
        ("inventree-token-provisioner", "inventree/token-provisioner"),
        ("keda", "keda"),
        ("kube-api-proxy", "kube-api-proxy"),
        ("kube-system", "kube-system"),
        ("kubevirt-cdi-operator", "kubevirt/cdi-operator"),
        ("kubevirt-operator", "kubevirt/operator"),
        ("kvm-device-plugin", "kvm-device-plugin"),
        ("langfuse-agent-rbac", "langfuse/agent-rbac"),
        ("langfuse-app", "langfuse/app"),
        ("langfuse-cache", "langfuse/cache"),
        ("langfuse-db", "langfuse/db"),
        ("litellm-db", "litellm/db"),
        ("activitywatch", "activitywatch"),
        ("agent-box", "agent-box/app"),
        ("agent-box-namespace", "agent-box/namespace"),
        ("agentplane-index-app", "agentplane-index/app"),
        ("agentplane-index-db", "agentplane-index/db"),
        ("agentplane-index-secrets", "agentplane-index/secrets"),
        ("agent-workspaces-app", "agents/agent-sandbox/workspaces/app"),
        ("agent-workspaces-namespace", "agents/agent-sandbox/workspaces/namespace"),
        ("airlock", "agents/airlock"),
        ("alloy-otlp-bearer", "agents/alloy-otlp-bearer"),
    )

    assert set(generators) == {artifact_name for artifact_name, _ in cases}
    for artifact_name, source_relative in cases:
        relative = f"cluster/k8s/{source_relative}"
        generator = generators[artifact_name]
        assert generator["spec"]["sources"] == [expected_sources.get(artifact_name, expected_source)]
        (artifact,) = generator["spec"]["artifacts"]
        assert artifact["name"] == artifact_name
        assert "revision" not in artifact  # Content-derived, not the monorepo revision.
        assert artifact["originRevision"] == "@repo"
        consumer = yaml.safe_load((root / f"{source_relative}/flux-kustomization.yaml").read_text())
        assert consumer["spec"]["sourceRef"] == {
            "kind": "ExternalArtifact",
            "name": artifact["name"],
            "namespace": generator["metadata"]["namespace"],
        }
        assert consumer["spec"]["path"] == f"./{relative}"
        primary_operation = next(
            operation for operation in artifact["copy"] if operation["to"] == f"@artifact/{relative}/"
        )
        assert primary_operation == {
            "from": f"@repo/{relative}/**",
            "to": f"@artifact/{relative}/",
            "exclude": ["flux-kustomization.yaml"],
        }
        assert f"{artifact_relative}/flux-kustomization.yaml" in root_kustomization["resources"]
        source = root / source_relative
        packaged_root = tmp_path / artifact_name
        packaged = packaged_root / relative
        for operation in artifact["copy"]:
            operation_source_relative = operation["from"].removeprefix("@repo/").removesuffix("/**")
            operation_source = root / operation_source_relative.removeprefix("cluster/k8s/")
            operation_target = packaged_root / operation["to"].removeprefix("@artifact/")
            shutil.copytree(
                operation_source, operation_target, ignore=shutil.ignore_patterns(*operation.get("exclude", []))
            )
        assert not (packaged / "flux-kustomization.yaml").exists()
        original = subprocess.run([kustomize, "build", str(source)], check=True, capture_output=True, text=True)
        rebuilt = subprocess.run([kustomize, "build", str(packaged)], check=True, capture_output=True, text=True)
        assert list(yaml.safe_load_all(rebuilt.stdout)) == list(yaml.safe_load_all(original.stdout))


def test_source_watcher_is_bootstrapped_with_shared_permissions() -> None:
    components = get_required_path("_main/cluster/k8s/flux-system/gotk-components.yaml")
    documents = list(yaml.safe_load_all(components.read_text()))
    deployment = next(d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == "source-watcher")
    assert deployment["metadata"]["namespace"] == "flux-system"
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "ghcr.io/fluxcd/source-watcher:v2.2.4"
    assert any(
        d["kind"] == "CustomResourceDefinition" and d["spec"]["names"]["kind"] == "ArtifactGenerator" for d in documents
    )
    binding = next(
        d
        for d in documents
        if d["kind"] == "ClusterRoleBinding" and d["metadata"]["name"] == "crd-controller-flux-system"
    )
    assert {"kind": "ServiceAccount", "name": "source-watcher", "namespace": "flux-system"} in binding["subjects"]


if __name__ == "__main__":
    pytest_bazel.main()
