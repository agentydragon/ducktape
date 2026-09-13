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
        # keep-sorted start
        ("activitywatch", "activitywatch"),
        ("agent-box", "agent-box/app"),
        ("agent-box-namespace", "agent-box/namespace"),
        ("agent-machine-access-tf", "agents/machine-access-tf"),
        ("agent-sandbox-controller", "agents/agent-sandbox/controller"),
        ("agent-shared-rbac", "agents/shared-rbac"),
        ("agent-shared-secrets", "agents/shared-secrets"),
        ("agent-workspaces-app", "agents/agent-sandbox/workspaces"),
        ("agentplane-crds", "agentplane-crds"),
        ("agentplane-egress-credentials", "agentplane-egress-credentials/secrets"),
        ("agentplane-egress-credentials-namespace", "agentplane-egress-credentials/namespace"),
        ("agentplane-index-app", "agentplane-index/app"),
        ("agentplane-index-db", "agentplane-index/db"),
        ("agentplane-index-namespace", "agentplane-index/namespace"),
        ("agentplane-index-secrets", "agentplane-index/secrets"),
        ("agentplane-staging", "agentplane-staging"),
        ("agentplane-testing", "agentplane-testing"),
        ("agents-mitmproxy-namespace", "agents/mitmproxy-namespace"),
        ("aiquota", "aiquota"),
        ("airlock", "agents/airlock"),
        ("alloy-otlp-bearer", "agents/alloy-otlp-bearer"),
        ("archivebox-app", "x/archivebox/app"),
        ("archivebox-namespace", "x/archivebox/namespace"),
        ("attic-jwt-rotation", "agents/attic-jwt-rotation"),
        ("atuin", "atuin/app"),
        ("atuin-db", "atuin/db"),
        ("atuin-namespace", "atuin/namespace"),
        ("atuin-secrets", "atuin/secrets"),
        ("atuin-user-provisioner", "atuin/user-provisioner"),
        ("augur-evidence", "forgejo/augur-evidence"),
        ("authelia-app", "x/authelia/app"),
        ("authelia-namespace", "x/authelia/namespace"),
        ("authentik", "authentik/app"),
        ("authentik-db", "authentik/db"),
        ("authentik-jwt-rotation", "agents/authentik-jwt-rotation"),
        ("authentik-namespace", "authentik/namespace"),
        ("authentik-proxy-routes", "authentik/proxy-routes"),
        ("authentik-secrets", "authentik/secrets"),
        ("authentik-servicemonitor", "authentik/servicemonitor"),
        ("authentik-sso-secrets", "authentik/sso-secrets"),
        ("browsertrix-app", "x/browsertrix/app"),
        ("browsertrix-bucket", "x/browsertrix/bucket"),
        ("browsertrix-namespace", "x/browsertrix/namespace"),
        ("browsertrix-retained", "x/browsertrix/retained"),
        ("budget-app", "budget/app"),
        ("budget-ledger", "forgejo/budget-ledger"),
        ("budget-namespace", "budget/namespace"),
        ("cdi", "kubevirt/cdi"),
        ("cert-manager", "cert-manager/app"),
        ("cert-manager-environment", "cert-manager/environment"),
        ("cert-manager-issuer-config", "cert-manager/issuer-config"),
        ("cert-manager-servicemonitor", "cert-manager/servicemonitor"),
        ("cert-manager-trust", "cert-manager/trust"),
        ("claude-rbac", "agents/agent-rbac-base"),
        ("claude-sandbox-secrets", "agents/claude-sandbox-secrets"),
        ("cli-proxy-api", "cli-proxy-api"),
        ("clickhouse", "clickhouse/cluster"),
        ("clickhouse-grafana", "analytics/grafana"),
        ("clickhouse-namespace", "clickhouse/namespace"),
        ("clickhouse-operator", "clickhouse/operator"),
        ("clickhouse-schema", "clickhouse/schema"),
        ("cnpg", "cnpg"),
        ("coinbase-read", "agents/coinbase-read"),
        ("coredns-custom", "coredns-custom"),
        ("cpap-data", "forgejo/cpap-data"),
        ("cpap-sync", "cpap-sync"),
        ("dcgm-exporter", "dcgm-exporter"),
        ("descheduler", "descheduler"),
        ("dns-automation", "dns-automation"),
        ("docker-ci", "docker-ci"),
        ("egress-proxy-rugged", "egress-proxy-rugged"),
        ("evidence-market-roster", "evidence"),
        ("external-creds", "external-creds"),
        ("external-secrets-config", "external-secrets/config"),
        ("external-secrets-operator", "external-secrets/operator"),
        ("firecrawl", "firecrawl/app"),
        ("firecrawl-db", "firecrawl/db"),
        ("firecrawl-namespace", "firecrawl/namespace"),
        ("flux-grafana-secrets", "flux-grafana-secrets"),
        ("flux-image-automation-forgejo", "flux-image-automation-forgejo"),
        ("flux-image-automation-ghcr", "flux-image-automation-ghcr"),
        ("flux-monitoring", "flux-monitoring"),
        ("flux-webhook", "flux-webhook"),
        ("flux-webhook-token", "flux-webhook-token"),
        ("forgejo", "forgejo/app"),
        ("forgejo-agentydragon", "forgejo/agentydragon"),
        ("forgejo-agentydragon-repos", "forgejo/agentydragon-repos"),
        ("forgejo-cache", "forgejo/cache"),
        ("forgejo-claude", "forgejo/claude"),
        ("forgejo-db", "forgejo/db"),
        ("forgejo-images", "forgejo-images"),
        ("forgejo-namespace", "forgejo/namespace"),
        ("forgejo-secrets", "forgejo/secrets"),
        ("forgejo-servicemonitor", "forgejo/servicemonitor"),
        ("forgejo-token-rotation", "agents/forgejo-token-rotation"),
        ("gateway", "gateway"),
        ("gatus", "gatus/app"),
        ("gatus-db", "gatus/db"),
        ("gatus-namespace", "gatus/namespace"),
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
        ("github-secrets-sync-secrets", "github-secrets-sync/secrets"),
        ("goldilocks", "goldilocks"),
        ("google-workspace-mcp", "x/google-workspace-mcp"),
        ("grafana-helmrepository", "monitoring/grafana-helmrepository"),
        ("grafana-instance", "monitoring/grafana-instance"),
        ("grafana-operator", "monitoring/grafana-operator"),
        ("grocy-mcp-sf", "grocy/sf/mcp"),
        ("grocy-mcp-sf-servicemonitor", "grocy/sf/mcp-servicemonitor"),
        ("grocy-mcp-vallejo", "grocy/vallejo/mcp"),
        ("grocy-mcp-vallejo-servicemonitor", "grocy/vallejo/mcp-servicemonitor"),
        ("grocy-sf", "grocy/sf/app"),
        ("grocy-sf-user-perms", "grocy/sf/user-perms"),
        ("grocy-vallejo", "grocy/vallejo/app"),
        ("grocy-vallejo-user-perms", "grocy/vallejo/user-perms"),
        ("ha-mcp", "agents/ha-mcp/app"),
        ("ha-mcp-credentials", "agents/ha-mcp/credentials"),
        ("ha-mcp-namespace", "agents/ha-mcp/namespace"),
        ("ha-mcp-servicemonitor", "agents/ha-mcp/servicemonitor"),
        ("haku-ci", "haku-ci"),
        ("haku-cloud-agent", "haku/cloud-agent-tf"),
        ("haku-console", "haku/console"),
        ("haku-console-db", "haku/console/db"),
        ("haku-console-migration", "haku/console/migration"),
        ("haku-console-namespace", "haku/console-namespace"),
        ("haku-console-servicemonitor", "haku/console/servicemonitor"),
        ("haku-dispatch-dispatcher", "x/haku/dispatch/dispatcher"),
        ("haku-dispatch-litellm", "x/haku/dispatch/litellm"),
        ("haku-egress-proxy", "agents/haku-egress-proxy"),
        ("haku-egress-proxy-namespace", "agents/haku-egress-proxy-namespace"),
        ("haku-mailbox-app", "haku/mailbox/app"),
        ("haku-mailbox-db", "haku/mailbox/db"),
        ("haku-mailbox-namespace", "haku/mailbox-namespace"),
        ("haku-managed-agent", "haku/managed-agent"),
        ("haku-namespace", "haku/namespace"),
        ("haku-openclaw-spike-app", "agents/haku-openclaw-spike/app"),
        ("haku-openclaw-spike-backup", "agents/haku-openclaw-spike/backup"),
        ("haku-openclaw-spike-namespace", "agents/haku-openclaw-spike/namespace"),
        ("haku-rbac", "haku/rbac"),
        ("haku-state", "forgejo/haku-state"),
        ("haku-ui-image-webhook", "haku/ui-image-webhook"),
        ("haku-workloads", "haku/workloads"),
        ("haku-workspaces-app", "haku/workspaces/app"),
        ("headlamp-app", "headlamp"),
        ("home-assistant", "home-assistant"),
        ("hubble-ui", "hubble-ui"),
        ("infra-drift", "infra-drift"),
        ("inventree-app", "inventree/app"),
        ("inventree-db", "inventree/db"),
        ("inventree-namespace", "inventree/namespace"),
        ("inventree-secrets", "inventree/secrets"),
        ("inventree-token-provisioner", "inventree/token-provisioner"),
        ("keda", "keda"),
        ("kube-api-proxy", "kube-api-proxy"),
        ("kube-system", "kube-system"),
        ("kubectl-machine-mcp", "agents/kubectl-machine-mcp/app"),
        ("kubectl-machine-mcp-namespace", "agents/kubectl-machine-mcp/namespace"),
        ("kubectl-passthrough-mcp", "agents/kubectl-passthrough-mcp/app"),
        ("kubectl-passthrough-mcp-namespace", "agents/kubectl-passthrough-mcp/namespace"),
        ("kubevirt", "kubevirt/app"),
        ("kubevirt-cdi-operator", "kubevirt/cdi-operator"),
        ("kubevirt-operator", "kubevirt/operator"),
        ("kvm-device-plugin", "kvm-device-plugin"),
        ("kyverno", "kyverno/app"),
        ("kyverno-policies", "kyverno/policies"),
        ("langfuse-agent-rbac", "langfuse/agent-rbac"),
        ("langfuse-app", "langfuse/app"),
        ("langfuse-cache", "langfuse/cache"),
        ("langfuse-db", "langfuse/db"),
        ("langfuse-namespace", "langfuse/namespace"),
        ("langfuse-secrets", "langfuse/secrets"),
        ("litellm", "litellm/app"),
        ("litellm-cheap-experiments-namespace", "litellm-cheap-experiments/namespace"),
        ("litellm-db", "litellm/db"),
        ("litellm-keys-tf", "litellm/keys-tf"),
        ("litellm-namespace", "litellm/namespace"),
        ("litellm-secrets", "litellm/secrets"),
        ("litellm-servicemonitor", "litellm/servicemonitor"),
        ("litellm-tana", "litellm/tana"),
        ("local-path-provisioner", "local-path-provisioner"),
        ("loki-read-proxy", "agents/loki-read-proxy"),
        ("manifold-mcp", "agents/manifold-mcp/app"),
        ("manifold-mcp-namespace", "agents/manifold-mcp/namespace"),
        ("manifold-mcp-servicemonitor", "agents/manifold-mcp/servicemonitor"),
        ("matrix-app", "matrix/app"),
        ("matrix-db", "matrix/db"),
        ("matrix-namespace", "matrix/namespace"),
        ("matrix-secrets", "matrix/secrets"),
        ("matrix-user-provisioner", "matrix/user-provisioner"),
        ("metrics-server", "metrics-server"),
        ("monitoring-alloy", "monitoring/alloy"),
        ("monitoring-alloy-otlp-bearer-token-tf", "monitoring/alloy-otlp-bearer-token-tf"),
        ("monitoring-cilium", "monitoring/cilium"),
        ("monitoring-etcd", "monitoring/etcd"),
        ("monitoring-grafana-db", "monitoring/grafana-db"),
        ("monitoring-loki", "monitoring/loki"),
        ("monitoring-mimir", "monitoring/mimir"),
        ("monitoring-namespace", "monitoring/namespace"),
        ("monitoring-rules", "monitoring/rules"),
        ("monitoring-stack", "monitoring/stack"),
        ("monitoring-tempo", "monitoring/tempo"),
        ("nix-cache", "nix-cache/app"),
        ("nix-cache-bootstrap", "nix-cache/bootstrap"),
        ("nix-cache-db", "nix-cache/db"),
        ("nix-cache-namespace", "nix-cache/namespace"),
        ("node-feature-discovery", "node-feature-discovery"),
        ("nvidia-device-plugin", "nvidia-device-plugin"),
        ("oci-cache-app", "oci-cache/app"),
        ("oci-cache-namespace", "oci-cache/namespace"),
        ("ollama-agent-rbac", "ollama/agent-rbac"),
        ("ollama-app", "ollama/app"),
        ("ollama-namespace", "ollama/namespace"),
        ("ollama-secrets", "ollama/secrets"),
        ("openebs-lvm", "openebs-lvm"),
        ("openhands-app", "x/openhands/app"),
        ("openhands-namespace", "x/openhands/namespace"),
        ("openhands-sandboxes", "x/openhands/sandboxes"),
        ("osm-mcp", "agents/osm-mcp"),
        ("paperless-app", "paperless/app"),
        ("paperless-cache", "paperless/cache"),
        ("paperless-db", "paperless/db"),
        ("paperless-namespace", "paperless/namespace"),
        ("paperless-secrets", "paperless/secrets"),
        ("plaid-db-mcp", "agents/plaid-db-mcp/app"),
        ("plaid-db-mcp-servicemonitor", "agents/plaid-db-mcp/servicemonitor"),
        ("plaid-mcp", "agents/plaid-mcp"),
        ("plaid-mcp-db", "agents/plaid-mcp/db"),
        ("plaid-mcp-namespace", "agents/plaid-mcp/namespace"),
        ("postscanmail-mcp", "agents/postscanmail-mcp/app"),
        ("postscanmail-mcp-namespace", "agents/postscanmail-mcp/namespace"),
        ("postscanmail-mcp-servicemonitor", "agents/postscanmail-mcp/servicemonitor"),
        ("proxmox-proxy", "proxmox-proxy"),
        ("public-coder-agent-app", "agents/public-coder-agent/app"),
        ("public-coder-agent-backup", "agents/public-coder-agent/backup"),
        ("public-coder-agent-devbox", "agents/public-coder-agent/devbox"),
        ("public-coder-agent-k8s-reader", "agents/public-coder-agent/k8s-reader"),
        ("public-coder-agent-namespace", "agents/public-coder-agent/namespace"),
        ("public-coder-agent-proxy", "agents/public-coder-agent/proxy"),
        ("public-coder-agent-sshpiper", "agents/public-coder-agent/sshpiper"),
        ("reflector", "reflector"),
        ("reloader", "reloader"),
        ("sdr", "sdr"),
        ("seaweedfs-cluster", "seaweedfs/cluster"),
        ("seaweedfs-csi", "seaweedfs-csi"),
        ("seaweedfs-drivefs-artifacts-bucket", "seaweedfs/drivefs-artifacts-bucket"),
        ("seaweedfs-filer-db", "seaweedfs/db"),
        ("seaweedfs-forgejo-bucket", "seaweedfs/forgejo-bucket"),
        ("seaweedfs-haku-openclaw-spike-backups-bucket", "seaweedfs/haku-openclaw-spike-backups-bucket"),
        ("seaweedfs-langfuse-bucket", "seaweedfs/langfuse-bucket"),
        ("seaweedfs-loom-gym-bucket", "seaweedfs/loom-gym-bucket"),
        ("seaweedfs-monitoring", "seaweedfs/monitoring"),
        ("seaweedfs-namespace", "seaweedfs/namespace"),
        ("seaweedfs-operator", "seaweedfs/operator"),
        ("seaweedfs-pr-visuals-bucket", "seaweedfs/pr-visuals-bucket"),
        ("seaweedfs-public-coder-agent-backups-bucket", "seaweedfs/public-coder-agent-backups-bucket"),
        ("seaweedfs-public-s3", "seaweedfs/public-s3"),
        ("seaweedfs-registry-cache-bucket", "seaweedfs/registry-cache-bucket"),
        ("seaweedfs-secrets", "seaweedfs/secrets"),
        ("seaweedfs-vm-images-bucket", "seaweedfs/vm-images-bucket"),
        ("seaweedfs-wayback-archive-bucket", "seaweedfs/wayback-archive-bucket"),
        ("ssh-mcp", "ssh-mcp"),
        ("ssh-mcp-secrets", "ssh-mcp/secrets"),
        ("sshpiper-crds", "sshpiper-crds"),
        ("sso-providers-tf", "authentik/sso-providers-tf"),
        ("study-casino-agent-rbac", "study-casino/agent-rbac"),
        ("study-casino-app", "study-casino/app"),
        ("study-casino-db", "study-casino/db"),
        ("study-casino-namespace", "study-casino/namespace"),
        ("talos-cloud-controller-manager", "talos-cloud-controller-manager"),
        ("tana-mcp", "agents/tana-mcp"),
        ("tana-mcp-facade", "agents/tana-mcp-facade"),
        ("tana-mcp-facade-monitoring", "agents/tana-mcp-facade/monitoring"),
        ("tandoor-app", "x/tandoor/app"),
        ("tandoor-db", "x/tandoor/db"),
        ("tandoor-namespace", "x/tandoor/namespace"),
        ("tofu-controller", "tofu-controller"),
        ("tofu-state-db", "tofu-state/db"),
        ("tofu-state-namespace", "tofu-state/namespace"),
        ("user-agentydragon", "user-agentydragon"),
        ("valkey", "valkey"),
        ("vector-talos-logs", "vector-talos-logs"),
        ("vm-images-publisher", "vm-images-publisher"),
        ("volsync", "volsync"),
        ("vpa", "vpa"),
        ("website", "website"),
        # keep-sorted end
    )

    artifact_names = [
        artifact["name"] for generator in generators.values() for artifact in generator["spec"]["artifacts"]
    ]
    assert len(artifact_names) == len(set(artifact_names)) == len(cases)
    generated_artifacts = {
        artifact["name"]: (generator, artifact)
        for generator in generators.values()
        for artifact in generator["spec"]["artifacts"]
    }
    assert set(generated_artifacts) == {artifact_name for artifact_name, _ in cases}
    for artifact_name, source_relative in cases:
        relative = f"cluster/k8s/{source_relative}"
        generator, artifact = generated_artifacts[artifact_name]
        assert generator["spec"]["sources"] == [expected_sources.get(artifact_name, expected_source)]
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
