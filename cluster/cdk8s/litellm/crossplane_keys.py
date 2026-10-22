"""Crossplane-managed LiteLLM teams and virtual keys."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount, k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)
from litellm_key_crds.io.crossplane.m.litellm import (
    Key,
    KeySpec,
    KeySpecForProvider,
    KeySpecForProviderKeyWoSecretRef,
    KeySpecForProviderMetadataSecretRef,
    KeySpecForProviderOrganizationIdRef,
    KeySpecForProviderTeamIdRef,
    KeySpecProviderConfigRef,
    KeySpecWriteConnectionSecretToRef,
)
from litellm_organization_crds.io.crossplane.m.litellm import (
    Organization,
    OrganizationSpec,
    OrganizationSpecForProvider,
    OrganizationSpecProviderConfigRef,
)
from litellm_providerconfig_crds.io.crossplane.m.litellm import (
    ProviderConfig,
    ProviderConfigSpec,
    ProviderConfigSpecCredentials,
    ProviderConfigSpecCredentialsSecretRef,
)
from litellm_team_crds.io.crossplane.m.litellm import (
    Team,
    TeamSpec,
    TeamSpecForProvider,
    TeamSpecForProviderRouterSettings,
    TeamSpecForProviderRouterSettingsFallbacks,
    TeamSpecProviderConfigRef,
)

from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from model_catalog.policies import KEY_MODEL_LANES

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/crossplane-keys"
NAME = "litellm-crossplane-keys"
NAMESPACE = "litellm"
_LITELLM_API_BASE = "http://litellm.litellm.svc.cluster.local:4000"
_PROVIDER_CONFIG_SECRET = "litellm-crossplane-provider-credentials"


def _lane_models(*names: str) -> list[str]:
    return [route.id for name in names for route in KEY_MODEL_LANES[name].allowed]


def _key_spec(
    *,
    alias: str,
    models: Sequence[str],
    metadata_secret: str,
    max_budget: int | None = None,
    budget_duration: str | None = None,
    team: str | None = None,
    connection_secret: str | None = None,
    client_secret: str | None = None,
) -> KeySpec:
    for_provider = KeySpecForProvider(
        key_alias=alias,
        models=list(models),
        metadata_secret_ref=KeySpecForProviderMetadataSecretRef(name=metadata_secret),
        organization_id_ref=KeySpecForProviderOrganizationIdRef(name="ducktape"),
        max_budget=max_budget,
        budget_duration=budget_duration,
        team_id_ref=KeySpecForProviderTeamIdRef(name=team) if team is not None else None,
        key_wo_secret_ref=(
            KeySpecForProviderKeyWoSecretRef(name=client_secret, key="api-key") if client_secret is not None else None
        ),
        # The value is intentionally fixed in the SOPS Secret. Bump this when that
        # credential changes so Upjet updates the remote key instead of ignoring it.
        key_wo_version="1" if client_secret is not None else None,
    )
    return KeySpec(
        for_provider=for_provider,
        provider_config_ref=KeySpecProviderConfigRef(kind="ProviderConfig", name="default"),
        write_connection_secret_to_ref=(
            KeySpecWriteConnectionSecretToRef(name=connection_secret) if connection_secret is not None else None
        ),
    )


def _team_spec(*, name: str, fallbacks: Sequence[str]) -> TeamSpec:
    return TeamSpec(
        for_provider=TeamSpecForProvider(
            team_alias=name.removeprefix("team-"),
            router_settings=(
                TeamSpecForProviderRouterSettings(
                    fallbacks=[TeamSpecForProviderRouterSettingsFallbacks(model="*", fallback_models=list(fallbacks))]
                )
                if fallbacks
                else None
            ),
        ),
        provider_config_ref=TeamSpecProviderConfigRef(kind="ProviderConfig", name="default"),
    )


def _publish_key(
    chart: Chart,
    *,
    reader: ServiceAccount,
    key_name: str,
    connection_secret: str,
    target_secret: str,
    data_key: str = "api-key",
    annotations: Mapping[str, str] | None = None,
) -> None:
    store = single_secret_store(
        chart,
        f"crossplane-key-{key_name}",
        reader=reader,
        source_namespace=NAMESPACE,
        source_secret=connection_secret,
        consumer_namespace=NAMESPACE,
    )
    ExternalSecret(
        chart,
        f"publish-key-{key_name}",
        metadata=ApiObjectMetadata(name=target_secret, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(store),
        data=[remote_data(connection_secret, "attribute.key", secret_key=data_key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            metadata=ExternalSecretSpecTargetTemplateMetadata(annotations=dict(annotations or {})), type="Opaque"
        ),
    )


@dataclass(frozen=True)
class _DynamicKey:
    name: str
    models: Sequence[str]
    consumer: str
    max_budget: int | None = None
    budget_duration: str | None = None
    target_secret: str | None = None
    data_key: str = "api-key"
    annotations: Mapping[str, str] = field(default_factory=dict)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)

    reader = ServiceAccount(
        chart,
        "provider-credentials-reader",
        metadata=ApiObjectMetadata(name="crossplane-provider-credentials-reader", namespace=NAMESPACE),
        automount_token=False,
    )
    provider_store = single_secret_store(
        chart,
        "litellm-crossplane-provider-credentials",
        reader=reader,
        source_namespace=NAMESPACE,
        source_secret="litellm-master-key",
        consumer_namespace=NAMESPACE,
    )
    ExternalSecret(
        chart,
        "provider-credentials",
        metadata=ApiObjectMetadata(name=_PROVIDER_CONFIG_SECRET, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(provider_store),
        data=[remote_data("litellm-master-key", "api-key", secret_key="api_key")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            type="Opaque", data={"credentials": ('{"api_base":"' + _LITELLM_API_BASE + '","api_key":"{{ .api_key }}"}')}
        ),
    )

    ProviderConfig(
        chart,
        "provider-config",
        metadata=ApiObjectMetadata(name="default", namespace=NAMESPACE),
        spec=ProviderConfigSpec(
            credentials=ProviderConfigSpecCredentials(
                source="Secret",
                secret_ref=ProviderConfigSpecCredentialsSecretRef(
                    name=_PROVIDER_CONFIG_SECRET, namespace=NAMESPACE, key="credentials"
                ),
            )
        ),
    )

    Organization(
        chart,
        "organization-ducktape",
        metadata=ApiObjectMetadata(name="ducktape", namespace=NAMESPACE),
        spec=OrganizationSpec(
            for_provider=OrganizationSpecForProvider(organization_alias="ducktape"),
            provider_config_ref=OrganizationSpecProviderConfigRef(kind="ProviderConfig", name="default"),
        ),
    )

    Team(
        chart,
        "team-codex-clients",
        metadata=ApiObjectMetadata(name="team-codex-clients", namespace=NAMESPACE),
        spec=_team_spec(
            name="team-codex-clients",
            fallbacks=[route.id for route in KEY_MODEL_LANES["codex_client_models"].fallbacks],
        ),
    )
    Team(
        chart,
        "team-gemini-clients",
        metadata=ApiObjectMetadata(name="team-gemini-clients", namespace=NAMESPACE),
        spec=_team_spec(
            name="team-gemini-clients",
            fallbacks=[route.id for route in KEY_MODEL_LANES["gemini_client_models"].fallbacks],
        ),
    )
    Team(
        chart,
        "team-antigravity-clients",
        metadata=ApiObjectMetadata(name="team-antigravity-clients", namespace=NAMESPACE),
        spec=_team_spec(
            name="team-antigravity-clients",
            fallbacks=[route.id for route in KEY_MODEL_LANES["antigravity_client_models"].fallbacks],
        ),
    )

    dynamic_keys = [
        _DynamicKey(
            name="cheap-experiments",
            models=_lane_models("cheap_experiments_models"),
            consumer="haku-console-temporary-agent-experiments",
            max_budget=50,
            budget_duration="30d",
            annotations={
                "description": "LiteLLM virtual key for temporary agent experiments; Mistral, Google, Ollama, Anthropic Haiku, and OpenAI Luna only"
            },
        ),
        _DynamicKey(
            name="buildbuddy-live-tests",
            models=_lane_models("cheap_experiments_models"),
            consumer="buildbuddy-live-openai-tests",
            max_budget=50,
            budget_duration="30d",
        ),
        _DynamicKey(
            name="disposable-env",
            models=_lane_models("qwen_client_models"),
            consumer="agentplane-disposable-env",
            annotations={
                "description": "LiteLLM virtual key for the disposable k3s test environment; local Ollama Qwen routes only"
            },
        ),
        _DynamicKey(
            name="agentplane-staging",
            models=_lane_models(
                "gpt6_oai_lane_models", "claude_client_models", "antigravity_client_models", "ollama_chat_client_models"
            ),
            consumer="agentplane-staging",
        ),
        _DynamicKey(
            name="codex-pod",
            models=_lane_models("gpt6_oai_lane_models"),
            consumer="codex-pod",
            max_budget=50,
            budget_duration="30d",
            annotations={
                "description": "LiteLLM virtual key for the codex-pod agent (chatgpt-sub/oai-responses/gpt-6-* models only); reflected into codex-pod as LITELLM_API_KEY",
                "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "codex-pod",
                "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "codex-pod",
            },
        ),
        _DynamicKey(
            name="public-coder-agent",
            models=_lane_models(
                "gpt6_codex_client_models",
                "gpt6_oai_lane_models",
                "gemini_client_models",
                "antigravity_client_models",
                "embedding_client_models",
            ),
            consumer="public-coder-agent",
            annotations={
                "description": "LiteLLM virtual key for public-coder-agent OpenClaw and least-credential runner-proxy-mediated Haku Console Codex shells; Gemini chat models, plus embeddings",
                "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "public-coder-agent,haku-console",
                "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "public-coder-agent,haku-console",
            },
        ),
        _DynamicKey(
            name="codex-pod-messages",
            models=_lane_models("gpt6_codex_client_models"),
            consumer="codex-pod codex-claude",
            target_secret="litellm-codex-pod-messages-key",
            data_key="CODEX_LITELLM_KEY",
            annotations={
                "description": "GPT-6-only LiteLLM Messages key for codex-pod; reflected as CODEX_LITELLM_KEY",
                "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "codex-pod",
                "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "codex-pod",
            },
        ),
        _DynamicKey(
            name="agent-workspaces-codex",
            models=_lane_models("gpt6_oai_lane_models"),
            consumer="retired standalone Codex workspaces; key retained for future use",
            annotations={
                "description": "LiteLLM virtual key for a future Codex workspace lane (chatgpt-sub/oai-responses/gpt-6-* models only); reflected into agent-workspaces if restored",
                "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "agent-workspaces",
                "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "agent-workspaces",
            },
        ),
    ]
    # Keep the canonical aliases unchanged during the ownership transfer. The old
    # Terraform Kustomization completes its apply (including deletes) before this one
    # starts, so there is no duplicate-alias window.
    for key in dynamic_keys:
        connection_secret = f"litellm-crossplane-key-{key.name}"
        metadata_secret = f"litellm-key-metadata-{key.name}"
        k8s.KubeSecret(
            chart,
            f"key-metadata-{key.name}",
            metadata=k8s.ObjectMeta(name=metadata_secret, namespace=NAMESPACE),
            type="Opaque",
            string_data={"consumer": key.consumer},
        )
        Key(
            chart,
            f"key-{key.name}",
            metadata=ApiObjectMetadata(name=f"key-{key.name}", namespace=NAMESPACE),
            spec=_key_spec(
                alias=key.name,
                models=key.models,
                metadata_secret=metadata_secret,
                max_budget=key.max_budget,
                budget_duration=key.budget_duration,
                connection_secret=connection_secret,
                team={
                    "codex-clients": "team-codex-clients",
                    "gemini-clients": "team-gemini-clients",
                    "antigravity-clients": "team-antigravity-clients",
                }.get(key.name),
            ),
        )
        _publish_key(
            chart,
            reader=reader,
            key_name=key.name,
            connection_secret=connection_secret,
            target_secret=key.target_secret or f"litellm-key-{key.name}",
            data_key=key.data_key,
            annotations=key.annotations,
        )

    for name, models, consumer, team in (
        ("claude-subscription-clients", "claude_client_models", "laptop-litellm-claude", None),
        ("codex-clients", "codex_client_models", "laptop-codex-claude, agent-box-codex", "team-codex-clients"),
        ("gemini-clients", "gemini_client_models", "laptop-gemini-claude", "team-gemini-clients"),
        ("antigravity-clients", "antigravity_client_models", "laptop-antigravity-claude", "team-antigravity-clients"),
    ):
        metadata_secret = f"litellm-key-metadata-{name}"
        k8s.KubeSecret(
            chart,
            f"key-metadata-{name}",
            metadata=k8s.ObjectMeta(name=metadata_secret, namespace=NAMESPACE),
            type="Opaque",
            string_data={"consumer": consumer},
        )
        Key(
            chart,
            f"key-{name}",
            metadata=ApiObjectMetadata(name=f"key-{name}", namespace=NAMESPACE),
            spec=_key_spec(
                alias=name,
                models=_lane_models(models),
                metadata_secret=metadata_secret,
                team=team,
                client_secret=f"litellm-client-key-{name}",
            ),
        )

    return chart


def resources(
    chart: Chart,
    directory: RenderedDirectory,
    provider: Kustomization,
    litellm: Kustomization,
    tofu_handoff: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="20m",
        wait=True,
        depends_on=flux_kustomization_depends_on_many(provider, litellm, tofu_handoff),
        description=(
            "Own LiteLLM teams and virtual keys in Crossplane after the provider, proxy, and Tofu handoff are ready."
        ),
    )
