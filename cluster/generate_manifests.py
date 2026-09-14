"""Generate the LiteLLM configuration manifests with Python cdk8s.

The model roster is deliberately ordinary Python: it can be imported by tests and
by the other configuration consumers, while cdk8s supplies both the application
YAML serialization and the final Kubernetes ConfigMap envelope. The generated YAML
is still a normal Flux/Kustomize input; this experiment does not give CI cluster
credentials or ask cdk8s to apply anything.
"""

import argparse
from pathlib import Path

from cdk8s import ApiObject, App, Chart, JsonPatch, Yaml
from cdk8s_plus_33 import ConfigMap

from cluster.k8s.litellm.app.model_rosters import (
    ANTHROPIC_MODELS,
    ASTRA_CONTEXT_WINDOW,
    ASTRA_MAX_TOKENS,
    CLIPROXY_MODELS,
    CODEX_CONTEXT_WINDOW,
    CODEX_MAX_TOKENS,
    CODEX_MEASURED_MODELS,
    GEMINI_EMBEDDING_MODELS,
    GEMINI_MODELS,
    MISTRAL_MODELS,
    TANA_MODELS,
    ZAI_ANTHROPIC_MODELS,
    ApiShape,
    Provider,
    exposed_name,
)
from tana.litellm_proxy.model_registry import TANA_LLM_PROXY_RESPONDING_MODELS

_OLLAMA_BASE = "http://ollama.ollama.svc.cluster.local:11434"
_CLIPROXY_BASE = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317"
_TANA_CUSTOM_HANDLER = """\
from __future__ import annotations

from tana.litellm_proxy.custom_handler import tana_handler

__all__ = ["tana_handler"]
"""


def _model_entry(
    model_name: str,
    model: str,
    mode: str,
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    supports_function_calling: bool = False,
    model_info: dict[str, int] | None = None,
    extra_body: dict | None = None,
) -> dict:
    """Build one LiteLLM model entry while omitting unset optional fields."""
    litellm_params: dict = {"model": model}
    if api_base is not None:
        litellm_params["api_base"] = api_base
    if api_key is not None:
        litellm_params["api_key"] = api_key
    if extra_body is not None:
        litellm_params["extra_body"] = extra_body

    info: dict = {"mode": mode}
    if supports_function_calling:
        info["supports_function_calling"] = True
    if model_info is not None:
        info.update(model_info)
    return {"model_name": model_name, "litellm_params": litellm_params, "model_info": info}


def _ollama_entries() -> list[dict]:
    entries: list[dict] = []
    for model, ollama_model, contexts in (
        ("gpt-oss-20b", "gpt-oss:20b", (128 * 1024, 256 * 1024, 512 * 1024, 1024 * 1024)),
        ("gpt-oss-120b", "gpt-oss:120b", (128 * 1024,)),
        ("gemma4-31b-it-q8_0", "gemma4:31b-it-q8_0", (128 * 1024,)),
    ):
        suffixes = [(f"{context // 1024}k" if context < 1024 * 1024 else "1m", context) for context in contexts]
        for suffix, context in suffixes:
            extra_body = None if context == 128 * 1024 else {"options": {"num_ctx": context}}
            entries.append(
                _model_entry(
                    exposed_name(Provider.OLLAMA, ApiShape.OAI_CHAT, f"{model}-{suffix}"),
                    f"openai/{ollama_model}",
                    "chat",
                    api_base=f"{_OLLAMA_BASE}/v1",
                    api_key="ollama",
                    supports_function_calling=True,
                    extra_body=extra_body,
                )
            )
        for suffix, context in suffixes:
            extra_body = None if context == 128 * 1024 else {"options": {"num_ctx": context}}
            entries.append(
                _model_entry(
                    exposed_name(Provider.OLLAMA, ApiShape.OLM_CHAT, f"{model}-{suffix}"),
                    f"ollama/{ollama_model}",
                    "chat",
                    api_base=_OLLAMA_BASE,
                    supports_function_calling=True,
                    extra_body=extra_body,
                )
            )

    entries.append(
        _model_entry(
            exposed_name(Provider.OLLAMA, ApiShape.OLM_EMBED, "qwen3-embedding-4b"),
            "ollama/qwen3-embedding:4b",
            "embedding",
            api_base=_OLLAMA_BASE,
        )
    )
    return entries


def _codex_model_info(model: str) -> dict[str, int]:
    if model == "gpt-6-astra":
        return {
            "max_input_tokens": ASTRA_CONTEXT_WINDOW,
            "max_output_tokens": ASTRA_MAX_TOKENS,
            "max_tokens": ASTRA_MAX_TOKENS,
        }
    if model in CODEX_MEASURED_MODELS:
        return {
            "max_input_tokens": CODEX_CONTEXT_WINDOW,
            "max_output_tokens": CODEX_MAX_TOKENS,
            "max_tokens": CODEX_MAX_TOKENS,
        }
    return {}


def _cliproxy_entries() -> list[dict]:
    return [
        _model_entry(
            exposed_name(Provider.CHATGPT, shape, model),
            f"{provider_model}/{model}",
            mode,
            api_base=api_base,
            api_key="os.environ/CLIPROXY_CLIENT_KEY",
            supports_function_calling=True,
            model_info=_codex_model_info(model),
        )
        for shape, provider_model, api_base, mode in (
            (ApiShape.ANT_MESSAGES, "anthropic", _CLIPROXY_BASE, "chat"),
            (ApiShape.OAI_RESPONSES, "openai", f"{_CLIPROXY_BASE}/v1", "responses"),
        )
        for model in CLIPROXY_MODELS
    ]


def _tana_entries() -> list[dict]:
    return [
        _model_entry(
            exposed_name(Provider.TANA, ApiShape.ANT_MESSAGES, exposed),
            f"anthropic/{downstream}",
            "chat",
            api_base="http://tana-litellm.litellm.svc.cluster.local:4000",
            api_key="os.environ/LITELLM_MASTER_KEY",
            supports_function_calling=True,
        )
        for exposed, downstream in TANA_MODELS
    ]


def _anthropic_entries() -> list[dict]:
    return [
        _model_entry(
            exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model),
            f"anthropic/{model}",
            "chat",
            api_base=_CLIPROXY_BASE,
            api_key="os.environ/CLIPROXY_CLIENT_KEY",
            supports_function_calling=True,
        )
        for model in ANTHROPIC_MODELS
    ] + [
        _model_entry(
            exposed_name(Provider.ANTHROPIC_API, ApiShape.ANT_MESSAGES, model),
            f"anthropic/{model}",
            "chat",
            api_key="os.environ/ANTHROPIC_API_KEY",
            supports_function_calling=True,
        )
        for model in ANTHROPIC_MODELS
    ]


def _simple_provider_entries() -> list[dict]:
    entries: list[dict] = [
        _model_entry(
            exposed_name(Provider.GROQ, ApiShape.OAI_CHAT, model),
            f"groq/{model}",
            "chat",
            api_key="os.environ/GROQ_API_KEY",
            supports_function_calling=True,
        )
        for model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant")
    ]
    entries.extend(
        _model_entry(model, f"groq/{model}", "audio_transcription", api_key="os.environ/GROQ_API_KEY")
        for model in ("whisper-large-v3", "whisper-large-v3-turbo")
    )
    entries.extend(
        _model_entry(
            exposed_name(Provider.GOOGLE, ApiShape.GOOG_GENERATE, model),
            f"gemini/{model}",
            "chat",
            api_key="os.environ/GEMINI_API_KEY",
            supports_function_calling=True,
        )
        for model in GEMINI_MODELS
    )
    entries.append(
        _model_entry(
            exposed_name(Provider.GOOGLE, ApiShape.GOOG_EMBED, GEMINI_EMBEDDING_MODELS[0]),
            f"gemini/{GEMINI_EMBEDDING_MODELS[0]}",
            "embedding",
            api_key="os.environ/GEMINI_API_KEY",
        )
    )
    # This unprefixed alias is part of the durable OpenClaw embedding index's
    # identity. It is intentionally retained until that index is rebuilt.
    entries.append(
        _model_entry(
            "gemini-embedding-2", "gemini/gemini-embedding-2", "embedding", api_key="os.environ/GEMINI_API_KEY"
        )
    )
    entries.append(
        _model_entry(
            exposed_name(Provider.GOOGLE, ApiShape.GOOG_EMBED, GEMINI_EMBEDDING_MODELS[1]),
            f"gemini/{GEMINI_EMBEDDING_MODELS[1]}",
            "embedding",
            api_key="os.environ/GEMINI_API_KEY",
        )
    )
    entries.extend(
        _model_entry(
            exposed_name(Provider.MISTRAL, ApiShape.OAI_CHAT, model),
            f"mistral/{model}",
            "chat",
            api_key="os.environ/MISTRAL_API_KEY",
            supports_function_calling=True,
        )
        for model in MISTRAL_MODELS
    )
    return entries


def main_proxy_config() -> dict:
    """Return the complete main-proxy config from the shared Python roster."""
    return {
        "model_list": [
            *_ollama_entries(),
            *_tana_entries(),
            *_cliproxy_entries(),
            *_anthropic_entries(),
            *_simple_provider_entries(),
        ],
        "litellm_settings": {"drop_params": True, "callbacks": ["langfuse_otel", "prometheus"]},
        "router_settings": {
            "model_group_alias": {
                "gpt-6-astra": {
                    "model": exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, "gpt-6-astra"),
                    "hidden": True,
                }
            }
        },
        "general_settings": {"forward_client_headers_to_llm_api": True, "store_model_in_db": False},
    }


def _yaml_config(config: dict) -> str:
    # cdk8s owns serialization of the application YAML as well as the
    # surrounding Kubernetes object. This is intentionally not PyYAML: the
    # app config is data, while cdk8s is the one manifest/YAML writer here.
    return Yaml.format_objects([config])


def _formatted_config_map_data(data: dict[str, object]) -> dict[str, str]:
    formatted: dict[str, str] = {}
    for filename, value in data.items():
        if filename == "config.yaml":
            assert isinstance(value, dict)
            formatted[filename] = _yaml_config(value)
        else:
            assert isinstance(value, str)
            formatted[filename] = value
    return formatted


def _tana_proxy_config() -> dict:
    return {
        "model_list": [
            {
                "model_name": model.model_id,
                "litellm_params": {"model": f"tana/tana/{model.model_id}", "custom_llm_provider": "tana"},
                "model_info": {"mode": "chat", "supports_function_calling": True},
            }
            for model in TANA_LLM_PROXY_RESPONDING_MODELS
        ],
        "litellm_settings": {
            "drop_params": True,
            "callbacks": ["langfuse_otel"],
            "custom_provider_map": [{"provider": "tana", "custom_handler": "custom_handler.tana_handler"}],
        },
    }


def _workers_proxy_config() -> dict:
    return {
        "model_list": [
            {
                "model_name": f"{model}-anthropic",
                "litellm_params": {
                    "model": f"litellm_proxy/{model}-anthropic",
                    "api_base": "http://litellm.litellm.svc.cluster.local:4000",
                    "api_key": "os.environ/ZAI_ZONE_KEY",
                },
            }
            for model in ZAI_ANTHROPIC_MODELS
        ],
        "litellm_settings": {"drop_params": True, "callbacks": ["prometheus"]},
        "general_settings": {"store_model_in_db": False},
    }


def _config_maps() -> tuple[tuple[str, str, str, dict[str, object]], ...]:
    return (
        ("litellm", "litellm-config", "litellm", {"config.yaml": main_proxy_config()}),
        (
            "tana-litellm",
            "tana-litellm-config",
            "litellm",
            {"config.yaml": _tana_proxy_config(), "custom_handler.py": _TANA_CUSTOM_HANDLER},
        ),
        ("workers-litellm", "workers-litellm-config", "haku-dispatch", {"config.yaml": _workers_proxy_config()}),
    )


def _api_resource(
    chart: Chart, resource_id: str, *, api_version: str, kind: str, metadata: dict, fields: dict[str, object]
) -> None:
    """Emit an unstructured Kubernetes resource through cdk8s's escape hatch."""
    resource = ApiObject(chart, resource_id, api_version=api_version, kind=kind, metadata=metadata)
    for field, value in fields.items():
        resource.add_json_patch(JsonPatch.add(f"/{field}", value))


def _metadata(
    name: str, namespace: str, *, labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None
) -> dict[str, object]:
    result: dict[str, object] = {"name": name, "namespace": namespace}
    if labels is not None:
        result["labels"] = labels
    if annotations is not None:
        result["annotations"] = annotations
    return result


def _literal_env(name: str, value: str) -> dict[str, object]:
    return {"name": name, "value": value}


def _secret_env(name: str, secret_name: str, key: str) -> dict[str, object]:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}}}


def _health_probe(path: str, initial_delay_seconds: int, failure_threshold: int) -> dict[str, object]:
    return {
        "httpGet": {"path": path, "port": "http"},
        "initialDelaySeconds": initial_delay_seconds,
        "periodSeconds": 10,
        "timeoutSeconds": 5,
        "failureThreshold": failure_threshold,
    }


def _litellm_container(
    *,
    image: str,
    env: list[dict[str, object]],
    startup_failure_threshold: int,
    config_items: list[dict[str, str]],
    resources: dict[str, object] | None = None,
    image_pull_policy: str | None = None,
) -> dict[str, object]:
    container: dict[str, object] = {
        "name": "litellm",
        "image": image,
        "args": ["--config", "/etc/litellm/config.yaml"],
        "ports": [{"name": "http", "containerPort": 4000, "protocol": "TCP"}],
        "env": env,
        "livenessProbe": _health_probe("/health/liveliness", 30, 3),
        "readinessProbe": _health_probe("/health/readiness", 10, 3),
        "startupProbe": _health_probe("/health/liveliness", 5, startup_failure_threshold),
        "volumeMounts": [{"name": "config", "mountPath": "/etc/litellm", "readOnly": True}],
    }
    if image_pull_policy is not None:
        container["imagePullPolicy"] = image_pull_policy
    if resources is not None:
        container["resources"] = resources
    return container


def _litellm_deployment(
    chart: Chart,
    *,
    name: str,
    namespace: str,
    image: str,
    replicas: int,
    env: list[dict[str, object]],
    config_map_name: str,
    startup_failure_threshold: int,
    config_items: list[dict[str, str]],
    resources: dict[str, object] | None = None,
    image_pull_policy: str | None = None,
    image_pull_secrets: list[dict[str, str]] | None = None,
    service_account_name: str | None = None,
    automount_service_account_token: bool | None = None,
    termination_grace_period_seconds: int | None = None,
    node_selector: dict[str, str] | None = None,
    tolerations: list[dict[str, object]] | None = None,
    topology_spread_constraints: list[dict[str, object]] | None = None,
    strategy: dict[str, object] | None = None,
) -> None:
    labels = {"app.kubernetes.io/name": name}
    pod_spec: dict[str, object] = {
        "containers": [
            _litellm_container(
                image=image,
                env=env,
                startup_failure_threshold=startup_failure_threshold,
                config_items=config_items,
                resources=resources,
                image_pull_policy=image_pull_policy,
            )
        ],
        "volumes": [{"name": "config", "configMap": {"name": config_map_name, "items": config_items}}],
    }
    if image_pull_secrets is not None:
        pod_spec["imagePullSecrets"] = image_pull_secrets
    if service_account_name is not None:
        pod_spec["serviceAccountName"] = service_account_name
    if automount_service_account_token is not None:
        pod_spec["automountServiceAccountToken"] = automount_service_account_token
    if termination_grace_period_seconds is not None:
        pod_spec["terminationGracePeriodSeconds"] = termination_grace_period_seconds
    if node_selector is not None:
        pod_spec["nodeSelector"] = node_selector
    if tolerations is not None:
        pod_spec["tolerations"] = tolerations
    if topology_spread_constraints is not None:
        pod_spec["topologySpreadConstraints"] = topology_spread_constraints

    spec: dict[str, object] = {
        "replicas": replicas,
        "selector": {"matchLabels": labels},
        "template": {"metadata": {"labels": labels}, "spec": pod_spec},
    }
    if strategy is not None:
        spec["strategy"] = strategy

    _api_resource(
        chart,
        f"{name}-deployment",
        api_version="apps/v1",
        kind="Deployment",
        metadata=_metadata(name, namespace, labels=labels, annotations={"reloader.stakater.com/auto": "true"}),
        fields={"spec": spec},
    )


def _service(
    chart: Chart,
    *,
    name: str,
    namespace: str,
    labels: dict[str, str] | None,
    target_port: str | int,
    include_type: bool,
    include_protocol: bool,
) -> None:
    port: dict[str, object] = {"name": "http", "port": 4000, "targetPort": target_port}
    if include_protocol:
        port["protocol"] = "TCP"
    spec: dict[str, object] = {"selector": {"app.kubernetes.io/name": name}, "ports": [port]}
    if include_type:
        spec["type"] = "ClusterIP"
    _api_resource(
        chart,
        f"{name}-service",
        api_version="v1",
        kind="Service",
        metadata=_metadata(name, namespace, labels=labels),
        fields={"spec": spec},
    )


def _http_route(chart: Chart, *, name: str, namespace: str, hostname: str) -> None:
    _api_resource(
        chart,
        f"{name}-httproute",
        api_version="gateway.networking.k8s.io/v1",
        kind="HTTPRoute",
        metadata=_metadata(name, namespace),
        fields={
            "spec": {
                "parentRefs": [{"name": "cluster-gateway", "namespace": "gateway-system"}],
                "hostnames": [hostname],
                "rules": [
                    {
                        "timeouts": {"request": "600s", "backendRequest": "600s"},
                        "backendRefs": [{"name": name, "port": 4000}],
                    }
                ],
            }
        },
    )


def _main_proxy_env() -> list[dict[str, object]]:
    return [
        _literal_env("HOST", "0.0.0.0"),
        _literal_env("PORT", "4000"),
        _secret_env("LITELLM_MASTER_KEY", "litellm-master-key", "api-key"),
        _secret_env("DATABASE_URL", "litellm-db-app", "uri"),
        _secret_env("LITELLM_SALT_KEY", "litellm-salt-key", "key"),
        _secret_env("ANTHROPIC_API_KEY", "litellm-anthropic-key", "api-key"),
        _secret_env("GROQ_API_KEY", "litellm-groq-key", "GROQ_API_KEY"),
        _secret_env("GEMINI_API_KEY", "litellm-gemini-key", "GEMINI_API_KEY"),
        _secret_env("MISTRAL_API_KEY", "litellm-mistral-key", "MISTRAL_API_KEY"),
        _secret_env("CLIPROXY_CLIENT_KEY", "litellm-cliproxy-key", "CLIPROXY_CLIENT_KEY"),
        _literal_env("LANGFUSE_OTEL_HOST", "http://langfuse-web.langfuse.svc.cluster.local:3000"),
        _secret_env("LANGFUSE_PUBLIC_KEY", "langfuse-secrets", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"),
        _secret_env("LANGFUSE_SECRET_KEY", "langfuse-secrets", "LANGFUSE_INIT_PROJECT_SECRET_KEY"),
    ]


def _tana_proxy_env() -> list[dict[str, object]]:
    return [
        _literal_env("HOST", "0.0.0.0"),
        _literal_env("PORT", "4000"),
        _secret_env("LITELLM_MASTER_KEY", "litellm-master-key", "api-key"),
        _secret_env("TANA_FIREBASE_REFRESH_TOKEN", "tana-firebase-refresh-token", "refresh_token"),
        _literal_env("LANGFUSE_OTEL_HOST", "http://langfuse-web.langfuse.svc.cluster.local:3000"),
        _secret_env("LANGFUSE_PUBLIC_KEY", "langfuse-secrets", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"),
        _secret_env("LANGFUSE_SECRET_KEY", "langfuse-secrets", "LANGFUSE_INIT_PROJECT_SECRET_KEY"),
    ]


def _workers_proxy_env() -> list[dict[str, object]]:
    return [
        _literal_env("HOST", "0.0.0.0"),
        _literal_env("PORT", "4000"),
        _secret_env("LITELLM_MASTER_KEY", "workers-litellm-master-key", "api-key"),
        _secret_env("DATABASE_URL", "haku-dispatch-db-app", "uri"),
        _secret_env("LITELLM_SALT_KEY", "workers-litellm-salt-key", "key"),
        _secret_env("ZAI_ZONE_KEY", "litellm-key-haku-lane-zai", "api-key"),
    ]


def _add_main_resources(chart: Chart) -> None:
    _litellm_deployment(
        chart,
        name="litellm",
        namespace="litellm",
        image="litellm/litellm:1.100.1",
        replicas=2,
        env=_main_proxy_env(),
        config_map_name="litellm-config",
        startup_failure_threshold=36,
        config_items=[{"key": "config.yaml", "path": "config.yaml"}],
        resources={"requests": {"cpu": "100m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}},
        service_account_name="litellm",
        termination_grace_period_seconds=90,
        node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
        tolerations=[{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}],
        topology_spread_constraints=[
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}},
            }
        ],
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
    )
    _service(
        chart,
        name="litellm",
        namespace="litellm",
        labels={"app.kubernetes.io/name": "litellm"},
        target_port="http",
        include_type=True,
        include_protocol=True,
    )
    _api_resource(
        chart,
        "litellm-serviceaccount",
        api_version="v1",
        kind="ServiceAccount",
        metadata=_metadata("litellm", "litellm"),
        fields={"automountServiceAccountToken": False},
    )
    _http_route(chart, name="litellm", namespace="litellm", hostname="litellm.allegedly.works")


def _add_tana_resources(chart: Chart) -> None:
    _api_resource(
        chart,
        "forgejo-images-creds",
        api_version="external-secrets.io/v1",
        kind="ExternalSecret",
        metadata=_metadata("forgejo-images-creds", "litellm"),
        fields={
            "spec": {
                "refreshInterval": "1h",
                "secretStoreRef": {"name": "kubernetes-forgejo-images-secret-store", "kind": "ClusterSecretStore"},
                "target": {
                    "name": "forgejo-images-creds",
                    "template": {"type": "kubernetes.io/dockerconfigjson", "mergePolicy": "Merge"},
                },
                "dataFrom": [{"extract": {"key": "forgejo-images-creds"}}],
            }
        },
    )
    _litellm_deployment(
        chart,
        name="tana-litellm",
        namespace="litellm",
        image="git.allegedly.works/ducktape-ci/tana-litellm-proxy:devel-20260913074202-77faa01",
        replicas=1,
        env=_tana_proxy_env(),
        config_map_name="tana-litellm-config",
        startup_failure_threshold=12,
        config_items=[
            {"key": "config.yaml", "path": "config.yaml"},
            {"key": "custom_handler.py", "path": "custom_handler.py"},
        ],
        image_pull_policy="Always",
        image_pull_secrets=[{"name": "forgejo-images-creds"}],
        automount_service_account_token=False,
        termination_grace_period_seconds=90,
    )
    _service(
        chart,
        name="tana-litellm",
        namespace="litellm",
        labels={"app.kubernetes.io/name": "tana-litellm"},
        target_port="http",
        include_type=True,
        include_protocol=True,
    )
    _http_route(chart, name="tana-litellm", namespace="litellm", hostname="tana-litellm.allegedly.works")


def _add_workers_resources(chart: Chart) -> None:
    _litellm_deployment(
        chart,
        name="workers-litellm",
        namespace="haku-dispatch",
        image="litellm/litellm:1.90.2",
        replicas=1,
        env=_workers_proxy_env(),
        config_map_name="workers-litellm-config",
        startup_failure_threshold=12,
        config_items=[{"key": "config.yaml", "path": "config.yaml"}],
        resources={"requests": {"cpu": "100m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "4Gi"}},
    )
    _service(
        chart,
        name="workers-litellm",
        namespace="haku-dispatch",
        labels=None,
        target_port=4000,
        include_type=False,
        include_protocol=False,
    )
    _api_resource(
        chart,
        "workers-litellm-network-policy",
        api_version="cilium.io/v2",
        kind="CiliumNetworkPolicy",
        metadata=_metadata("workers-litellm-zone-pods-only", "haku-dispatch"),
        fields={
            "spec": {
                "endpointSelector": {"matchLabels": {"app.kubernetes.io/name": "workers-litellm"}},
                "ingress": [
                    {
                        "fromEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "haku-sandbox-zai"}}],
                        "toPorts": [{"ports": [{"port": "4000", "protocol": "TCP"}]}],
                    },
                    {
                        "fromEndpoints": [
                            {
                                "matchLabels": {
                                    "k8s:io.kubernetes.pod.namespace": "haku-dispatch",
                                    "k8s:app.kubernetes.io/name": "dispatcher",
                                }
                            }
                        ],
                        "toPorts": [{"ports": [{"port": "4000", "protocol": "TCP"}]}],
                    },
                ],
                "egress": [
                    {
                        "toEndpoints": [
                            {"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}
                        ],
                        "toPorts": [{"ports": [{"port": "53", "protocol": "UDP"}, {"port": "53", "protocol": "TCP"}]}],
                    },
                    {
                        "toEndpoints": [
                            {
                                "matchLabels": {
                                    "k8s:io.kubernetes.pod.namespace": "litellm",
                                    "k8s:app.kubernetes.io/name": "litellm",
                                }
                            }
                        ],
                        "toPorts": [{"ports": [{"port": "4000", "protocol": "TCP"}]}],
                    },
                    {
                        "toEndpoints": [
                            {
                                "matchLabels": {
                                    "k8s:io.kubernetes.pod.namespace": "haku-dispatch",
                                    "k8s:cnpg.io/cluster": "haku-dispatch-db",
                                }
                            }
                        ],
                        "toPorts": [{"ports": [{"port": "5432", "protocol": "TCP"}]}],
                    },
                ],
            }
        },
    )


def _add_service_monitor(chart: Chart) -> None:
    _api_resource(
        chart,
        "litellm-servicemonitor",
        api_version="monitoring.coreos.com/v1",
        kind="ServiceMonitor",
        metadata=_metadata("litellm", "litellm"),
        fields={
            "spec": {
                "selector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}},
                "endpoints": [
                    {
                        "port": "http",
                        "path": "/metrics",
                        "interval": "15s",
                        "scrapeTimeout": "10s",
                        "bearerTokenSecret": {"name": "litellm-master-key", "key": "api-key"},
                    }
                ],
            }
        },
    )


def generate_manifests(output_directory: Path) -> None:
    """Synthesize ordinary Kubernetes ConfigMaps into output_directory."""
    output_directory.mkdir(parents=True, exist_ok=True)

    app = App(outdir=str(output_directory))
    for chart_name, config_map_name, namespace, data in _config_maps():
        chart = Chart(app, chart_name, disable_resource_name_hashes=True)
        ConfigMap(
            chart,
            "config",
            metadata={
                "name": config_map_name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": "cdk8s", "app.kubernetes.io/part-of": "litellm"},
                "annotations": {
                    "ducktape.dev/generated": "by cdk8s under Bazel",
                    "ducktape.dev/delivery": "Flux can consume this ordinary Kubernetes YAML",
                },
            },
            data=_formatted_config_map_data(data),
        )
        if chart_name == "litellm":
            _add_main_resources(chart)
        elif chart_name == "tana-litellm":
            _add_tana_resources(chart)
        elif chart_name == "workers-litellm":
            _add_workers_resources(chart)
        else:
            raise ValueError(f"Unhandled chart: {chart_name}")

    service_monitor_chart = Chart(app, "litellm-servicemonitor", disable_resource_name_hashes=True)
    _add_service_monitor(service_monitor_chart)
    app.synth()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    generate_manifests(args.output_dir.resolve())


if __name__ == "__main__":
    main()
