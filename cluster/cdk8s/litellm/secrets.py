"""The litellm namespace's provider credentials: ESO copies of canonical keys held
elsewhere, owned by the `litellm` Kustomization.

Hand-written beside the generated output: `litellm-master-key.sops.yaml` and
`litellm-salt-key.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import ExternalSecret, ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s.external_secrets.external_secret import add_external_secret, cluster_secret_store, remote_data
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/secrets"
_NAME = "litellm-secrets"
_NAMESPACE = "litellm"
_EXTERNAL_CREDS_STORE = "kubernetes-external-creds-secret-store"
_SOPS_FILES = ("litellm-master-key.sops.yaml", "litellm-salt-key.sops.yaml")


def _external_secret(
    chart: Chart,
    id: str,
    *,
    name: str,
    target: str,
    store: str,
    secret_key: str,
    source: str,
    source_property: str,
    annotations: dict[str, str] | None = None,
) -> ExternalSecret:
    return add_external_secret(
        chart,
        id,
        name=name,
        namespace=_NAMESPACE,
        refresh="1h",
        store=cluster_secret_store(store),
        data=[remote_data(source, source_property, secret_key=secret_key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        target_name=target,
        annotations=annotations,
    )


def _chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    # Consumer-owned referent identity for canonical credentials approved by
    # source-side RoleBindings in external-creds.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=_NAMESPACE)
    )
    # Anthropic API key for LiteLLM's anthropic-api/ant-messages/* exposed models. Its dedicated
    # SecretStore can read only ducktape-flux/llm-anthropic-haku.
    _external_secret(
        chart,
        "anthropic",
        name="litellm-anthropic-key",
        target="litellm-anthropic-key",
        store=_EXTERNAL_CREDS_STORE,
        secret_key="api-key",
        source="llm-anthropic-haku",
        source_property="api-key",
    )
    # Mirrors the CLIProxyAPI client key (SSOT: cli-proxy-api/cli-proxy-api-client-key) into the
    # litellm namespace so the main LiteLLM proxy can authenticate the codex-* upstream models
    # via CLIPROXY_CLIENT_KEY. ESO mirror -- we do not widen the declarative reflector
    # annotations on the source Secret.
    _external_secret(
        chart,
        "cliproxy",
        name="cliproxy-client-key",
        target="litellm-cliproxy-key",
        store="kubernetes-cli-proxy-api-secret-store",
        secret_key="CLIPROXY_CLIENT_KEY",
        source="cli-proxy-api-client-key",
        source_property="client-key",
        annotations={
            "description": (
                "CLIProxyAPI client key mirrored into litellm for the codex-* upstream models, sourced"
                " from cli-proxy-api/cli-proxy-api-client-key via kubernetes-cli-proxy-api-secret-store."
            )
        },
    )
    for provider, env, source in (
        ("groq", "GROQ_API_KEY", "llm-groq"),
        ("gemini", "GEMINI_API_KEY", "llm-gemini"),
        # The canonical Mistral credential lives in ducktape-flux.
        ("mistral", "MISTRAL_API_KEY", "llm-mistral"),
    ):
        _external_secret(
            chart,
            provider,
            name=f"litellm-{provider}-key",
            target=f"litellm-{provider}-key",
            store=_EXTERNAL_CREDS_STORE,
            secret_key=env,
            source=source,
            source_property="api-key",
        )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, _chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[*_SOPS_FILES, f"{_NAME}.k8s.yaml"])
    )
