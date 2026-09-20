"""Typed cdk8s synthesis for source-watcher ArtifactGenerator resources.

The artifact inventory is explicit here. The validation suite checks that each
entry has exactly one active Flux consumer and that its copy operations preserve
the consumer's rendered Kustomize resources.
"""

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Yaml
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGenerator,
    ArtifactGeneratorSpec,
    ArtifactGeneratorSpecArtifacts,
    ArtifactGeneratorSpecArtifactsCopy,
    ArtifactGeneratorSpecSources,
    ArtifactGeneratorSpecSourcesKind,
)

from cluster.cdk8s.flux import NAMESPACE, flux_kustomization, kustomize_kustomization

_ARTIFACT_GENERATORS_DIR = "cluster/k8s/artifact-generators"
_DUCKTAPE_SOURCE = ArtifactGeneratorSpecSources(
    alias="repo", kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY, name="ducktape", namespace=NAMESPACE
)
_FLUX_SYSTEM_SOURCE = ArtifactGeneratorSpecSources(
    alias="repo", kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY, name="flux-system", namespace="flux-system"
)

# name -> source directories copied into the output artifact, in copy order.
# The first path is the consumer's Kustomization directory; following paths are
# shared bases that its Kustomization references.
_DUCKTAPE_ARTIFACTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("agentplane-staging", ("cluster/k8s/agentplane-staging",)),
    ("monitoring-stack", ("cluster/k8s/monitoring/stack",)),
    ("ntfy", ("cluster/k8s/ntfy",)),
    ("agentplane-testing", ("cluster/k8s/agentplane-testing",)),
    ("claude-rbac", ("cluster/k8s/agents/agent-rbac-base",)),
    ("agent-machine-access-tf", ("cluster/k8s/agents/machine-access-tf",)),
    ("authentik", ("cluster/k8s/authentik/app",)),
    ("sso-providers-tf", ("cluster/k8s/authentik/sso-providers-tf",)),
    ("cert-manager", ("cluster/k8s/cert-manager/app",)),
    (
        "cert-manager-environment",
        (
            "cluster/k8s/cert-manager/environment",
            "cluster/k8s/cert-manager/config",
            "cluster/k8s/cert-manager/cluster-ca",
        ),
    ),
    ("cert-manager-issuer-config", ("cluster/k8s/cert-manager/issuer-config",)),
    ("cnpg", ("cluster/k8s/cnpg",)),
    ("external-secrets-config", ("cluster/k8s/external-secrets/config",)),
    ("external-secrets-operator", ("cluster/k8s/external-secrets/operator",)),
    ("forgejo", ("cluster/k8s/forgejo/app",)),
    ("forgejo-images", ("cluster/k8s/forgejo-images",)),
    ("gateway", ("cluster/k8s/gateway",)),
    ("kyverno", ("cluster/k8s/kyverno/app",)),
    ("litellm-keys-tf", ("cluster/k8s/litellm/keys-tf",)),
    ("local-path-provisioner", ("cluster/k8s/local-path-provisioner",)),
    ("reflector", ("cluster/k8s/reflector",)),
    ("seaweedfs-cluster", ("cluster/k8s/seaweedfs/cluster",)),
    ("seaweedfs-filer-db", ("cluster/k8s/seaweedfs/db",)),
    ("seaweedfs-namespace", ("cluster/k8s/seaweedfs/namespace",)),
    ("seaweedfs-operator", ("cluster/k8s/seaweedfs/operator",)),
    ("seaweedfs-secrets", ("cluster/k8s/seaweedfs/secrets",)),
    ("tofu-controller", ("cluster/k8s/tofu-controller",)),
    ("tofu-state-db", ("cluster/k8s/tofu-state/db",)),
    ("valkey", ("cluster/k8s/valkey",)),
    ("kyverno-policies", ("cluster/k8s/kyverno/policies",)),
    ("haku-state", ("cluster/k8s/forgejo/haku-state",)),
    ("agentplane-crds", ("cluster/k8s/agentplane-crds",)),
    ("monitoring-namespace", ("cluster/k8s/monitoring/namespace",)),
    ("haku-namespace", ("cluster/k8s/haku/namespace",)),
    ("langfuse-namespace", ("cluster/k8s/langfuse/namespace",)),
    ("volsync", ("cluster/k8s/volsync",)),
    ("authentik-namespace", ("cluster/k8s/authentik/namespace",)),
    ("cert-manager-trust", ("cluster/k8s/cert-manager/trust",)),
    ("agent-sandbox-controller", ("cluster/k8s/agents/agent-sandbox/controller",)),
    ("grafana-helmrepository", ("cluster/k8s/monitoring/grafana-helmrepository",)),
    ("kubevirt", ("cluster/k8s/kubevirt/app",)),
    ("public-coder-agent-namespace", ("cluster/k8s/agents/public-coder-agent/namespace",)),
    ("agentplane-index", ("cluster/k8s/agentplane-index",)),
    ("clickhouse", ("cluster/k8s/clickhouse/cluster",)),
    ("forgejo-namespace", ("cluster/k8s/forgejo/namespace",)),
    ("github-secrets-sync-secrets", ("cluster/k8s/github-secrets-sync/secrets",)),
    ("grafana-instance", ("cluster/k8s/monitoring/grafana-instance",)),
    ("haku-egress-proxy", ("cluster/k8s/agents/haku-egress-proxy",)),
    ("haku-rbac", ("cluster/k8s/haku/rbac",)),
    ("langfuse-secrets", ("cluster/k8s/langfuse/secrets",)),
    ("litellm-namespace", ("cluster/k8s/litellm/namespace",)),
    ("agentplane-egress-credentials-namespace", ("cluster/k8s/agentplane-egress-credentials/namespace",)),
    ("agentplane-egress-credentials", ("cluster/k8s/agentplane-egress-credentials/secrets",)),
    ("litellm-secrets", ("cluster/k8s/litellm/secrets",)),
    ("matrix-namespace", ("cluster/k8s/matrix/namespace",)),
    ("seaweedfs-csi", ("cluster/k8s/seaweedfs-csi",)),
    ("seaweedfs-public-s3", ("cluster/k8s/seaweedfs/public-s3",)),
    ("seaweedfs-external-credentials", ("cluster/k8s/seaweedfs/external-credentials",)),
    ("study-casino-namespace", ("cluster/k8s/study-casino/namespace",)),
    ("tana-mcp", ("cluster/k8s/agents/tana-mcp",)),
    ("authentik-jwt-rotation", ("cluster/k8s/agents/authentik-jwt-rotation",)),
    ("cdi", ("cluster/k8s/kubevirt/cdi",)),
    ("flux-image-automation-ghcr", ("cluster/k8s/flux-image-automation-ghcr",)),
    ("gatus-namespace", ("cluster/k8s/gatus/namespace",)),
    ("grafana-operator", ("cluster/k8s/monitoring/grafana-operator",)),
    ("grocy-sf", ("cluster/k8s/grocy/sf/app", "cluster/k8s/grocy/app-base")),
    ("grocy-vallejo", ("cluster/k8s/grocy/vallejo/app", "cluster/k8s/grocy/app-base")),
    ("haku-console-namespace", ("cluster/k8s/haku/console-namespace",)),
    ("haku-egress-proxy-namespace", ("cluster/k8s/agents/haku-egress-proxy-namespace",)),
    ("haku-mailbox-namespace", ("cluster/k8s/haku/mailbox-namespace",)),
    ("haku-openclaw-spike-namespace", ("cluster/k8s/agents/haku-openclaw-spike/namespace",)),
    ("home-assistant", ("cluster/k8s/home-assistant",)),
    ("litellm", ("cluster/k8s/litellm/app",)),
    ("nix-cache", ("cluster/k8s/nix-cache",)),
    ("nvidia-device-plugin", ("cluster/k8s/nvidia-device-plugin",)),
    ("nvidia-runtimeclass", ("cluster/k8s/nvidia-runtimeclass",)),
    ("claude-sandbox-secrets", ("cluster/k8s/agents/claude-sandbox-secrets",)),
    ("coinbase-read", ("cluster/k8s/agents/coinbase-read",)),
    ("forgejo-token-rotation", ("cluster/k8s/agents/forgejo-token-rotation",)),
    ("ha-mcp", ("cluster/k8s/agents/ha-mcp/app",)),
    ("haku-openclaw-spike-app", ("cluster/k8s/agents/haku-openclaw-spike/app",)),
    ("haku-openclaw-spike-backup", ("cluster/k8s/agents/haku-openclaw-spike/backup",)),
    ("kubectl-passthrough-mcp", ("cluster/k8s/agents/kubectl-passthrough-mcp/app",)),
    ("loki-read-proxy", ("cluster/k8s/agents/loki-read-proxy",)),
    ("agents-mitmproxy-namespace", ("cluster/k8s/agents/mitmproxy-namespace",)),
    ("plaid-mcp", ("cluster/k8s/agents/plaid-mcp",)),
    ("public-coder-agent-app", ("cluster/k8s/agents/public-coder-agent/app",)),
    ("public-coder-agent-backup", ("cluster/k8s/agents/public-coder-agent/backup",)),
    ("activitywatch", ("cluster/k8s/activitywatch",)),
    ("agent-workspaces-app", ("cluster/k8s/agents/agent-sandbox/workspaces",)),
    ("airlock", ("cluster/k8s/agents/airlock",)),
    ("alloy-otlp-bearer", ("cluster/k8s/agents/alloy-otlp-bearer",)),
    ("public-coder-agent-devbox", ("cluster/k8s/agents/public-coder-agent/devbox",)),
    ("public-coder-agent-proxy", ("cluster/k8s/agents/public-coder-agent/proxy",)),
    ("public-coder-agent-sshpiper", ("cluster/k8s/agents/public-coder-agent/sshpiper",)),
    ("agent-shared-rbac", ("cluster/k8s/agents/shared-rbac",)),
    ("agent-shared-secrets", ("cluster/k8s/agents/shared-secrets",)),
    ("aiquota", ("cluster/k8s/aiquota",)),
    ("clickhouse-grafana", ("cluster/k8s/grafana",)),
    ("atuin", ("cluster/k8s/atuin",)),
    ("atuin-user-provisioner", ("cluster/k8s/atuin/user-provisioner",)),
    ("authentik-db", ("cluster/k8s/authentik/db",)),
    ("authentik-db-backups", ("cluster/k8s/authentik/db-backups",)),
    ("authentik-proxy-routes", ("cluster/k8s/authentik/proxy-routes",)),
    ("cli-proxy-api", ("cluster/k8s/cli-proxy-api",)),
    ("clickhouse-namespace", ("cluster/k8s/clickhouse/namespace",)),
    ("clickhouse-operator", ("cluster/k8s/clickhouse/operator",)),
    ("clickhouse-schema", ("cluster/k8s/clickhouse/schema",)),
    ("coredns-custom", ("cluster/k8s/coredns-custom",)),
    ("cpap-sync", ("cluster/k8s/cpap-sync",)),
    ("dcgm-exporter", ("cluster/k8s/dcgm-exporter",)),
    ("descheduler", ("cluster/k8s/descheduler",)),
    ("dns-automation", ("cluster/k8s/dns-automation",)),
    ("evidence-market-roster", ("cluster/k8s/evidence",)),
    ("flux-grafana-secrets", ("cluster/k8s/flux-grafana-secrets",)),
    ("flux-image-automation-forgejo", ("cluster/k8s/flux-image-automation-forgejo",)),
    ("flux-monitoring", ("cluster/k8s/flux-monitoring",)),
    ("flux-webhook", ("cluster/k8s/flux-webhook",)),
    ("flux-webhook-token", ("cluster/k8s/flux-webhook-token",)),
    ("forgejo-agentydragon", ("cluster/k8s/forgejo/agentydragon",)),
    ("forgejo-agentydragon-repos", ("cluster/k8s/forgejo/agentydragon-repos",)),
    ("budget-ledger", ("cluster/k8s/forgejo/budget-ledger",)),
    ("budget-namespace", ("cluster/k8s/forgejo/budget-namespace",)),
    ("forgejo-cache", ("cluster/k8s/forgejo/cache",)),
    ("forgejo-claude", ("cluster/k8s/forgejo/claude",)),
    ("cpap-data", ("cluster/k8s/forgejo/cpap-data",)),
    ("forgejo-db", ("cluster/k8s/forgejo/db",)),
    ("gatus", ("cluster/k8s/gatus/app",)),
    ("gatus-db", ("cluster/k8s/gatus/db",)),
    ("gatus-sso-tf", ("cluster/k8s/gatus/sso-tf",)),
    ("github-api-proxy", ("cluster/k8s/github-api-proxy/app",)),
    ("github-api-proxy-identity", ("cluster/k8s/github-api-proxy/identity",)),
    ("github-branch-protection", ("cluster/k8s/github-branch-protection",)),
    ("github-exporter", ("cluster/k8s/github-exporter",)),
    ("github-secrets-sync", ("cluster/k8s/github-secrets-sync",)),
    ("goldilocks", ("cluster/k8s/goldilocks",)),
    (
        "grocy-mcp-sf",
        ("cluster/k8s/grocy/sf/mcp", "cluster/k8s/grocy/mcp-base", "cluster/k8s/grocy/mcp-servicemonitor-base"),
    ),
    ("grocy-sf-user-perms", ("cluster/k8s/grocy/sf/user-perms", "cluster/k8s/grocy/user-perms-base")),
    (
        "grocy-mcp-vallejo",
        ("cluster/k8s/grocy/vallejo/mcp", "cluster/k8s/grocy/mcp-base", "cluster/k8s/grocy/mcp-servicemonitor-base"),
    ),
    ("grocy-vallejo-user-perms", ("cluster/k8s/grocy/vallejo/user-perms", "cluster/k8s/grocy/user-perms-base")),
    ("haku-console", ("cluster/k8s/haku/console",)),
    ("haku-mailbox-app", ("cluster/k8s/haku/mailbox/app",)),
    ("haku-mailbox-db", ("cluster/k8s/haku/mailbox/db",)),
    ("haku-forgejo-tea", ("cluster/k8s/haku/forgejo-tea",)),
    ("haku-ui-image-webhook", ("cluster/k8s/haku/ui-image-webhook",)),
    ("haku-workloads", ("cluster/k8s/haku/workloads",)),
    ("haku-workspaces-app", ("cluster/k8s/haku/workspaces/app",)),
    ("haku-ci", ("cluster/k8s/haku-ci",)),
    ("headlamp-app", ("cluster/k8s/headlamp",)),
    ("hubble-ui", ("cluster/k8s/hubble-ui",)),
    ("infra-drift", ("cluster/k8s/infra-drift",)),
    ("keda", ("cluster/k8s/keda",)),
    ("kube-api-proxy", ("cluster/k8s/kube-api-proxy",)),
    ("kube-system", ("cluster/k8s/kube-system",)),
    ("kubevirt-cdi-operator", ("cluster/k8s/kubevirt/cdi-operator",)),
    ("kubevirt-operator", ("cluster/k8s/kubevirt/operator",)),
    ("kvm-device-plugin", ("cluster/k8s/kvm-device-plugin",)),
    ("langfuse-app", ("cluster/k8s/langfuse/app",)),
    ("langfuse-seaweed", ("cluster/k8s/langfuse/seaweed",)),
    ("langfuse-cache", ("cluster/k8s/langfuse/cache",)),
    ("langfuse-db", ("cluster/k8s/langfuse/db",)),
    ("litellm-db", ("cluster/k8s/litellm/db",)),
    ("matrix-app", ("cluster/k8s/matrix/app",)),
    ("matrix-db", ("cluster/k8s/matrix/db",)),
    ("matrix-user-provisioner", ("cluster/k8s/matrix/user-provisioner",)),
    ("metrics-server", ("cluster/k8s/metrics-server",)),
    ("monitoring-alloy", ("cluster/k8s/monitoring/alloy",)),
    ("monitoring-alloy-otlp-bearer-token-tf", ("cluster/k8s/monitoring/alloy-otlp-bearer-token-tf",)),
    ("monitoring-cilium", ("cluster/k8s/monitoring/cilium",)),
    ("monitoring-etcd", ("cluster/k8s/monitoring/etcd",)),
    ("monitoring-grafana-db", ("cluster/k8s/monitoring/grafana-db",)),
    ("monitoring-loki", ("cluster/k8s/monitoring/loki",)),
    ("monitoring-mimir", ("cluster/k8s/monitoring/mimir",)),
    ("monitoring-rules", ("cluster/k8s/monitoring/rules",)),
    ("monitoring-tempo", ("cluster/k8s/monitoring/tempo",)),
    ("node-feature-discovery", ("cluster/k8s/node-feature-discovery",)),
    ("oci-cache", ("cluster/k8s/oci-cache",)),
    ("ollama-app", ("cluster/k8s/ollama",)),
    ("openebs-lvm", ("cluster/k8s/openebs-lvm",)),
    ("proxmox-proxy", ("cluster/k8s/proxmox-proxy",)),
    ("reloader", ("cluster/k8s/reloader",)),
    ("seaweedfs-drivefs-artifacts-bucket", ("cluster/k8s/seaweedfs/drivefs-artifacts-bucket",)),
    ("seaweedfs-forgejo-bucket", ("cluster/k8s/seaweedfs/forgejo-bucket",)),
    ("seaweedfs-langfuse-bucket", ("cluster/k8s/seaweedfs/langfuse-bucket",)),
    ("seaweedfs-loom-gym-bucket", ("cluster/k8s/seaweedfs/loom-gym-bucket",)),
    ("seaweedfs-monitoring", ("cluster/k8s/seaweedfs/monitoring",)),
    ("seaweedfs-pr-visuals-bucket", ("cluster/k8s/seaweedfs/pr-visuals-bucket",)),
    ("seaweedfs-public-coder-agent-backups-bucket", ("cluster/k8s/seaweedfs/public-coder-agent-backups-bucket",)),
    ("seaweedfs-registry-cache-bucket", ("cluster/k8s/seaweedfs/registry-cache-bucket",)),
    ("ssh-mcp", ("cluster/k8s/ssh-mcp",)),
    ("study-casino-app", ("cluster/k8s/study-casino/app",)),
    ("study-casino-db", ("cluster/k8s/study-casino/db",)),
    ("talos-cloud-controller-manager", ("cluster/k8s/talos-cloud-controller-manager",)),
    ("tofu-state-namespace", ("cluster/k8s/tofu-state/namespace",)),
    ("user-agentydragon", ("cluster/k8s/user-agentydragon",)),
    ("vector-talos-logs", ("cluster/k8s/vector-talos-logs",)),
    ("vm-images-publisher", ("cluster/k8s/vm-images-publisher",)),
    ("vpa", ("cluster/k8s/vpa",)),
    ("website", ("cluster/k8s/website",)),
)
_FLUX_SYSTEM_ARTIFACTS: tuple[tuple[str, tuple[str, ...]], ...] = (("external-creds", ("cluster/k8s/external-creds",)),)


def _copy_operation(source_path: str) -> ArtifactGeneratorSpecArtifactsCopy:
    return ArtifactGeneratorSpecArtifactsCopy(from_=f"@repo/{source_path}/**", to=f"@artifact/{source_path}/")


def _artifacts(definitions: tuple[tuple[str, tuple[str, ...]], ...]) -> list[ArtifactGeneratorSpecArtifacts]:
    return [
        ArtifactGeneratorSpecArtifacts(
            name=name, origin_revision="@repo", copy=[_copy_operation(path) for path in source_paths]
        )
        for name, source_paths in definitions
    ]


def artifact_generators(flux_chart: Chart, root: Path) -> Kustomization:
    """Synthesize the ArtifactGenerator CRs and return their Flux consumer."""
    out_dir = root / _ARTIFACT_GENERATORS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    app = App(outdir=str(out_dir))
    chart = Chart(app, "artifact-generators", disable_resource_name_hashes=True)
    for name, definitions, source in (
        ("ducktape-artifacts", _DUCKTAPE_ARTIFACTS, _DUCKTAPE_SOURCE),
        ("flux-system-artifacts", _FLUX_SYSTEM_ARTIFACTS, _FLUX_SYSTEM_SOURCE),
    ):
        ArtifactGenerator(
            chart,
            name,
            metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
            spec=ArtifactGeneratorSpec(artifacts=_artifacts(definitions), sources=[source]),
        )
    app.synth()

    kustomization = flux_kustomization(
        flux_chart,
        "artifact-generators",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=f"./{_ARTIFACT_GENERATORS_DIR}",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace=NAMESPACE
            ),
            depends_on=[KustomizationSpecDependsOn(name="flux-system", namespace="flux-system")],
        ),
    )
    (out_dir / "kustomization.yaml").write_text(
        Yaml.format_objects([kustomize_kustomization(resources=["artifact-generators.k8s.yaml"])])
    )
    return kustomization
