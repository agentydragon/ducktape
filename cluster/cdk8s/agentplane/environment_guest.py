"""Runtime configuration shared by KubeVirt and Firecracker guests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentplane.env import config as configuration
from agentplane.env.config import (
    CA_BUNDLE,
    JAVA_TRUST_STORE,
    CodexReadiness,
    Configuration,
    Endpoint,
    EnvironmentRuntimeControl,
    Harness,
    SVIDEndpoint,
    VMEnvironment,
    VMExecutor,
)
from agentplane.environment_runtime_transport.config import HISTORY_SERVICE_URI, MTLS, TLS
from agentplane.sandboxes.runtimeconfig.model_catalog import WORKSPACE_ROOT
from cluster.cdk8s.agentplane import history_service, llm_ingress, platform_routes, sandbox_pod, telemetry
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.cert_manager.cluster_ca import BUNDLE_KEY as ENVIRONMENT_RUNTIME_TRUST_BUNDLE_KEY

_ENVIRONMENT_RUNTIME_PORT = 9090
_ENVIRONMENT_RUNTIME_TLS_PATH = "/run/agentplane/environment-runtime-tls"
_ENVIRONMENT_RUNTIME_TRUST_PATH = "/run/agentplane/environment-runtime-trust"
_ENVIRONMENT_RUNTIME_ENV_FILE = "environment-runtime.env"


def guest_config_data(
    env: Environment,
    *,
    gateway: str,
    workload_api_socket: str | None = None,
    block_devices: Sequence[str],
    platform_instructions: str,
    harness_env: Mapping[str, str],
    session_reconcile_concurrency: int | None = None,
    codex_readiness: CodexReadiness | None = None,
) -> dict[str, str]:
    resources = env.vm_resources
    if resources is None:
        raise ValueError("VM Sandboxes require vm_resources")
    namespace = env.namespace

    def guest_address(port: int) -> str:
        return f"{gateway}:{port}"

    proxy = f"http://{guest_address(sandbox_pod.SIDECAR_LISTEN_PORT)}"
    no_proxy = f"127.0.0.1,localhost,{gateway}"
    network = {
        **dict.fromkeys(sandbox_pod.PROXY_VAR_NAMES, proxy),
        **dict.fromkeys(sandbox_pod.NO_PROXY_VAR_NAMES, no_proxy),
        **dict.fromkeys((*sandbox_pod.CA_BUNDLE_VAR_NAMES, "NIX_SSL_CERT_FILE", "PIP_CERT"), str(CA_BUNDLE)),
    }
    llm_url = llm_ingress.proxied_url(namespace)
    config = Configuration(
        version=2,
        session_reconcile_concurrency=session_reconcile_concurrency,
        codex_readiness=codex_readiness,
        state_dir="/state",
        workspace_root=WORKSPACE_ROOT,
        initialization_cwd=WORKSPACE_ROOT,
        control=EnvironmentRuntimeControl(
            listen_address=f"0.0.0.0:{_ENVIRONMENT_RUNTIME_PORT}",
            security=MTLS(
                tls=TLS(workload_api_socket=workload_api_socket)
                if workload_api_socket
                else TLS(
                    cert_file=f"{_ENVIRONMENT_RUNTIME_TLS_PATH}/tls.crt",
                    key_file=f"{_ENVIRONMENT_RUNTIME_TLS_PATH}/tls.key",
                    ca_file=f"{_ENVIRONMENT_RUNTIME_TRUST_PATH}/{ENVIRONMENT_RUNTIME_TRUST_BUNDLE_KEY}",
                )
            ),
        ),
        ingress=Endpoint(
            target=f"{history_service.service(namespace).fqdn}:{history_service.service(namespace).pod_port}",
            svid=SVIDEndpoint(
                server_uri=HISTORY_SERVICE_URI, proxy_address=guest_address(sandbox_pod.SIDECAR_LISTEN_PORT)
            ),
        ),
        model_config_target=guest_address(platform_routes.model_config(namespace).listen_port),
        platform_instructions=platform_instructions,
        environment_runtime_environment=configuration.EnvironmentVariables(values=network),
        harness_environment=configuration.EnvironmentVariables(
            values={
                **network,
                **env.sandbox_workload_env,
                **harness_env,
                "HOME": "/state/home",
                "USER": "agent",
                "LOGNAME": "agent",
                "PATH": "/run/current-system/sw/bin:/run/agentplane/environment/bin",
                "TZDIR": "/usr/share/zoneinfo",
                "JAVA_TOOL_OPTIONS": f"-Djavax.net.ssl.trustStore={JAVA_TRUST_STORE} -Djavax.net.ssl.trustStoreType=PKCS12",
            }
        ),
        claude=Harness(binary="/run/current-system/sw/bin/claude", base_url=llm_url, token_env="ANTHROPIC_AUTH_TOKEN"),
        codex=Harness(binary="/run/current-system/sw/bin/codex", base_url=f"{llm_url}/v1", token_env="OPENAI_API_KEY"),
        environment=VMEnvironment(
            resources=resources,
            console_autologin=env.vm_console_autologin,
            agent_uid=107,
            agent_gid=107,
            block_devices=block_devices,
            executor=VMExecutor(url="ws://127.0.0.1:4501") if resources.executor is not None else None,
        ),
    )
    return {
        "environment-runtime.json": config.model_dump_json(exclude_none=True),
        _ENVIRONMENT_RUNTIME_ENV_FILE: "".join(
            f'{name}="{value}"\n'
            for name, value in sorted(
                telemetry.service_environment(
                    env.namespace,
                    "agentplane-environment-runtime",
                    endpoint=platform_routes.telemetry_endpoint(host=gateway),
                ).items()
            )
        ),
    }
