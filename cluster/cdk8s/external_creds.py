"""Build source-side grants; see external_creds.md for the ownership contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, Secret, ServiceAccount
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import sops_decryption, write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAMESPACE = "ducktape-flux"
OUTPUT_DIR = "cluster/k8s/external-creds"
_READER_SERVICE_ACCOUNT = "external-creds-reader"


@dataclass(frozen=True)
class ApprovedConsumer:
    """One explicitly approved ServiceAccount and its stable RoleBinding name."""

    namespace: str
    binding_name: str


@dataclass(frozen=True)
class Credential:
    """Non-secret metadata needed to render a credential's source-side grant."""

    secret_file: str
    secret_name: str
    consumers: tuple[ApprovedConsumer, ...] = ()
    namespace: str = NAMESPACE


# This roster is the authorization boundary: each consumer is an explicit approval.
# Analysis AI has a source Secret but no consumer grant yet.
CREDENTIALS = (
    Credential(secret_file="analysis-ai.sops.yaml", secret_name="analysis-ai-api-key"),
    Credential(
        secret_file="anthropic-haku.sops.yaml",
        secret_name="llm-anthropic-haku",
        consumers=(
            ApprovedConsumer("litellm", "llm-anthropic-haku-litellm-reader"),
            ApprovedConsumer("flux-system", "llm-anthropic-haku-flux-reader"),
        ),
    ),
    Credential(
        secret_file="aws-route53-cert-manager.sops.yaml",
        secret_name="aws-route53-cert-manager-credentials",
        consumers=(ApprovedConsumer("cert-manager", "aws-route53-cert-manager-credentials-cert-manager-reader"),),
    ),
    Credential(
        secret_file="aws-route53-dns-automation.sops.yaml",
        secret_name="aws-route53-dns-automation-credentials",
        consumers=(ApprovedConsumer("flux-system", "aws-route53-dns-automation-credentials-flux-system-reader"),),
    ),
    Credential(
        secret_file="coinbase-api-credentials.sops.yaml",
        secret_name="coinbase-api-credentials",
        consumers=(
            ApprovedConsumer("coinbase-read", "coinbase-api-credentials-coinbase-read-reader"),
            ApprovedConsumer("haku-sandbox", "coinbase-api-credentials-haku-sandbox-reader"),
        ),
    ),
    Credential(
        secret_file="gemini.sops.yaml",
        secret_name="llm-gemini",
        consumers=(ApprovedConsumer("litellm", "llm-gemini-litellm-reader"),),
    ),
    Credential(
        secret_file="github-agentydragon-agent.sops.yaml",
        secret_name="github-agentydragon-agent",
        consumers=(
            ApprovedConsumer("public-coder-agent", "github-agentydragon-agent-public-coder-agent-reader"),
            ApprovedConsumer("haku-egress-proxy", "github-agentydragon-agent-haku-egress-proxy-reader"),
            ApprovedConsumer("haku-console", "github-agentydragon-agent-haku-console-reader"),
            ApprovedConsumer("monitoring", "github-agentydragon-agent-monitoring-reader"),
            ApprovedConsumer(
                "agentplane-egress-credentials", "github-agentydragon-agent-agentplane-egress-credentials-reader"
            ),
        ),
    ),
    Credential(
        secret_file="github-agentydragon-2.sops.yaml",
        secret_name="github-agentydragon-2",
        consumers=(
            ApprovedConsumer("flux-system", "github-agentydragon-2-flux-system-reader"),
            ApprovedConsumer("agents-infra", "github-agentydragon-2-agents-infra-reader"),
            ApprovedConsumer("nix-cache", "github-agentydragon-2-nix-cache-reader"),
            ApprovedConsumer("monitoring", "github-agentydragon-2-monitoring-reader"),
        ),
    ),
    Credential(
        secret_file="github-agentydragon.sops.yaml",
        secret_name="github-agentydragon",
        consumers=(
            ApprovedConsumer("flux-system", "github-agentydragon-flux-system-reader"),
            ApprovedConsumer("agents-infra", "github-agentydragon-agents-infra-reader"),
            ApprovedConsumer("nix-cache", "github-agentydragon-nix-cache-reader"),
            ApprovedConsumer("monitoring", "github-agentydragon-monitoring-reader"),
        ),
    ),
    Credential(
        secret_file="groq.sops.yaml",
        secret_name="llm-groq",
        consumers=(ApprovedConsumer("litellm", "llm-groq-litellm-reader"),),
    ),
    Credential(
        secret_file="mistral.sops.yaml",
        secret_name="llm-mistral",
        consumers=(ApprovedConsumer("litellm", "llm-mistral-litellm-reader"),),
    ),
)


def kustomize_resources() -> list[str]:
    """Return generated source-side RBAC and canonical SOPS files."""
    return ["external-creds.k8s.yaml", *(credential.secret_file for credential in CREDENTIALS)]


def chart(app: App) -> Chart:
    """Build exact-name, get-only credential reader Roles and RoleBindings in one chart."""
    chart = Chart(app, "external-creds", disable_resource_name_hashes=True)
    for credential in CREDENTIALS:
        if credential.consumers:
            role_name = f"{credential.secret_name}-reader"
            Role(
                chart,
                f"role-{credential.secret_name}",
                metadata=metadata(role_name, credential.namespace),
                rules=[
                    RolePolicyRule(
                        resources=[
                            Secret.from_secret_name(
                                chart, f"secret-{credential.secret_name}-reader", credential.secret_name
                            )
                        ],
                        verbs=["get"],
                    )
                ],
            )
            for index, consumer in enumerate(credential.consumers):
                RoleBinding(
                    chart,
                    f"binding-{credential.secret_name}-{index}",
                    metadata=metadata(consumer.binding_name, credential.namespace),
                    role=Role.from_role_name(chart, f"role-ref-{credential.secret_name}-{index}", role_name),
                ).add_subjects(
                    ServiceAccount.from_service_account_name(
                        chart,
                        f"service-account-ref-{credential.secret_name}-{index}",
                        _READER_SERVICE_ACCOUNT,
                        namespace_name=consumer.namespace,
                    )
                )
    return chart


def external_creds(flux_chart: Chart, root: Path, claude_rbac: Kustomization) -> Kustomization:
    """Generate credential grants and Kustomize wiring; source manifests stay hand-written."""
    resources = kustomize_resources()
    write_charts(root, OUTPUT_DIR, chart)

    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    kustomization = flux_kustomization(
        flux_chart,
        "external-creds",
        spec=KustomizationSpec(
            interval="10m",
            path=f"./{OUTPUT_DIR}",
            prune=True,
            decryption=sops_decryption(resources),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="external-creds", namespace=NAMESPACE
            ),
            depends_on=[flux_kustomization_depends_on(claude_rbac)],
            timeout="5m",
        ),
    )
    write_yaml(out_dir / "kustomization.yaml", kustomize_kustomization(resources=resources))
    return kustomization
