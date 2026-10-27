"""The Pod shape every agentplane box shares: the egress sidecar that relays a box's
traffic to the central proxy, the tokens only it mounts, the interception CA over the system bundle
and as Java's trust store, a kubeconfig that reaches the API server through the proxy, and the proxy
environment that points a workload at the sidecar. The runner SandboxTemplates (runner_sandbox.py) and
the sandbox Actions' box templates (command_sandbox.py) are both built from it, so both kinds of box sit
behind the same egress path; only the command box also gets the system bazelrc that points Bazel at
the trust store.

The recipe speaks core Pod types (`cdk8s_plus_34.k8s`); `render_pod_template` turns them into the
Kubernetes JSON a template holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ConfigMap, Role, RolePolicyRule, k8s
from constructs import Construct

from agentplane.egress import sidecar_config
from agentplane.egress.platform_routes import PlatformRoute
from agentplane.egress.resources import placeholder_of
from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE
from agentplane.runner.configuration import CA_BUNDLE, JAVA_TRUST_STORE, KUBECONFIG
from agentplane.runner_gateway.api import RUNNER_CONTROL_AUDIENCE
from agentplane.sandboxes.kubernetes_grants import RoleBindingGrant, RoleRef
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s.agentplane import egress, notifications, telemetry
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME
from cluster.cdk8s.settings_file import PodWiring, SettingsFile
from util.settings_contract import validate_settings_fragment

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_EGRESS_SIDECAR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"
# Mounted by the egress sidecar and no other container; every token under it is the Pod's own.
_EGRESS_TOKEN_DIR = "/var/run/agentplane-egress"
# Audiences the central proxy may substitute this Pod's identity for, and the file each is projected
# to under `_EGRESS_TOKEN_DIR`. The volume and the sidecar's mapping are both rendered from this, so
# neither can name a file the other does not project. The hop token is deliberately absent: it
# carries the proxy's own audience, so it is not substitutable anywhere.
_SUBSTITUTABLE_AUDIENCE_FILES = {
    egress.KUBERNETES_AUDIENCE: "kubernetes-token",
    notifications.TOKEN_AUDIENCE: "notifications-token",
    RUNNER_CONTROL_AUDIENCE: "runner-control-token",
    RUNNER_INGRESS_AUDIENCE: "runner-ingress-token",
}
SIDECAR_LISTEN_PORT = 3128
# The sidecar's settings file: a ConfigMap per Pod template, mounted on the sidecar alone.
SIDECAR_SETTINGS_VOLUME_NAME = "egress-sidecar-settings"
EGRESS_TOKEN_VOLUME_NAME = "egress-token"
_SIDECAR_SETTINGS_PATH = "/etc/agentplane-egress-sidecar/settings.yaml"
# Shared by a workload's egress-ca volumeMount and the pod-level volume -- Kubernetes matches the
# two by this name.
_EGRESS_CA_VOLUME_NAME = "egress-ca"
_MITM_PROXY_URL = f"http://127.0.0.1:{SIDECAR_LISTEN_PORT}"
_NO_PROXY_HOSTS = "127.0.0.1,localhost"
_CA_BUNDLE_PATH = str(CA_BUNDLE)
# Shared Java trust store; Bazel's embedded JDK reads it only when told to.
_JAVA_TRUST_STORE_PATH = JAVA_TRUST_STORE
# Configuration files for the box's tools, one key each, mounted file by file.
TOOL_CONFIG_MAP_NAME = "sandbox-tool-config"
TOOL_CONFIG_READER_ROLE_NAME = "agentplane-sandbox-tool-config-reader"
# The grant catalog entry and the PolicySet that let a Sandbox read the tool config.
TOOL_CONFIG_GRANT = "sandbox-tool-config"
_TOOL_CONFIG_VOLUME_NAME = "tool-config"
_BAZELRC_KEY = "bazel.bazelrc"
_KUBECONFIG_KEY = "kubeconfig"
# Outside the home directory: a subPath mount creates its missing parents as root, and kubectl keeps
# its cache under ~/.kube.
_KUBECONFIG_PATH = KUBECONFIG
PROXY_VAR_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
NO_PROXY_VAR_NAMES = ("NO_PROXY", "no_proxy")
CA_BUNDLE_VAR_NAMES = ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "REQUESTS_CA_BUNDLE")


def audience_token_file(audience: str) -> str:
    """The sidecar-only file holding this Pod's projected token for `audience`."""
    return f"{_EGRESS_TOKEN_DIR}/{_SUBSTITUTABLE_AUDIENCE_FILES[audience]}"


def egress_env() -> list[k8s.EnvVar]:
    """The proxy environment, for the workload container itself.

    The box's fence lets it reach DNS and the egress proxy and nothing else, so a process that does
    not know to use the proxy has no egress at all -- and anything entering by another door
    (`kubectl exec`, the sandbox Actions' exec, a debug shell) is that kind of process. Both
    spellings, since clients disagree on case; NO_PROXY is loopback and nothing else. KUBECONFIG
    names the box's own identity at the API server, through the same proxy.
    """
    return [*proxy_environment(), k8s.EnvVar(name="KUBECONFIG", value=_KUBECONFIG_PATH)]


def proxy_environment() -> list[k8s.EnvVar]:
    """Proxy routing and certificate trust for a workload that has no platform config mount."""
    return [
        *(k8s.EnvVar(name=name, value=_MITM_PROXY_URL) for name in PROXY_VAR_NAMES),
        *(k8s.EnvVar(name=name, value=_NO_PROXY_HOSTS) for name in NO_PROXY_VAR_NAMES),
        *(k8s.EnvVar(name=name, value=_CA_BUNDLE_PATH) for name in CA_BUNDLE_VAR_NAMES),
    ]


def workload_environment(env: Environment, existing: Sequence[k8s.EnvVar]) -> list[k8s.EnvVar]:
    """Add the deployment's non-secret defaults to one workload container."""
    existing_env_names = {item.name for item in existing}
    conflicting_env_names = existing_env_names & env.sandbox_workload_env.keys()
    if conflicting_env_names:
        names = ", ".join(sorted(conflicting_env_names))
        raise ValueError(f"sandbox workload environment defaults conflict with container env: {names}")
    return [*existing, *(k8s.EnvVar(name=name, value=value) for name, value in env.sandbox_workload_env.items())]


def egress_proxy_mounts() -> list[k8s.VolumeMount]:
    """The interception CA bundle needed to verify proxied requests."""
    return [
        k8s.VolumeMount(
            name=_EGRESS_CA_VOLUME_NAME, mount_path=_CA_BUNDLE_PATH, sub_path=egress.CA_BUNDLE_KEY, read_only=True
        )
    ]


def platform_control_mounts() -> list[k8s.VolumeMount]:
    """The kubeconfig for workload tools which need this Sandbox's API identity."""
    return [
        k8s.VolumeMount(
            name=_TOOL_CONFIG_VOLUME_NAME, mount_path=_KUBECONFIG_PATH, sub_path=_KUBECONFIG_KEY, read_only=True
        )
    ]


def proxy_trust_mounts() -> list[k8s.VolumeMount]:
    """The proxy CA at system and Java trust paths, without the Sandbox API identity."""
    return [
        *egress_proxy_mounts(),
        # System trust also covers tools which do not read CA environment variables.
        k8s.VolumeMount(
            name=_EGRESS_CA_VOLUME_NAME,
            mount_path="/etc/ssl/certs/ca-certificates.crt",
            sub_path=egress.CA_BUNDLE_KEY,
            read_only=True,
        ),
        k8s.VolumeMount(
            name=_EGRESS_CA_VOLUME_NAME,
            mount_path=_JAVA_TRUST_STORE_PATH,
            sub_path=egress.JAVA_TRUST_STORE_KEY,
            read_only=True,
        ),
    ]


def egress_mounts() -> list[k8s.VolumeMount]:
    """Proxy trust and platform control mounts needed by sandbox tools."""
    return [*proxy_trust_mounts(), *platform_control_mounts()]


def bazelrc_mount() -> k8s.VolumeMount:
    """Bazel's system rc, for a box that only runs commands. A runner leaves it out: crossing a
    container's memory limit kills every process in it, and a build that did would take the agent's
    harness down with it."""
    return k8s.VolumeMount(
        name=_TOOL_CONFIG_VOLUME_NAME, mount_path="/etc/bazel.bazelrc", sub_path=_BAZELRC_KEY, read_only=True
    )


def kubeconfig(ca_path: str) -> dict[str, object]:
    """A box's identity at the API server: through the proxy, verified against the bundle at
    `ca_path`, which carries the interception root, with the placeholder the proxy swaps for the box's
    own projected token. The box has no token of its own to put there; the sidecar holds it."""
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "in-cluster",
                "cluster": {"server": f"https://{egress.KUBERNETES_HOST}", "certificate-authority": ca_path},
            }
        ],
        "users": [{"name": "workload", "user": {"token": placeholder_of(egress.KUBERNETES_CREDENTIAL)}}],
        "contexts": [{"name": "in-cluster", "context": {"cluster": "in-cluster", "user": "workload"}}],
        "current-context": "in-cluster",
    }


def add_tool_config(scope: Construct, env: Environment) -> None:
    """Bazel's system rc, which Bazel in a command box reads before its workspace's own, and the
    kubeconfig. Bazel's JVM fetches through the proxy but trusts only its own store, which lacks the
    interception root; and Bazel scrubs a test's environment, so the box's egress environment reaches
    a test only when named here."""
    passthrough = " ".join(f"--test_env={var.name}" for var in egress_env())
    tool_config_data = {
        _BAZELRC_KEY: (
            f"startup --host_jvm_args=-Djavax.net.ssl.trustStore={_JAVA_TRUST_STORE_PATH}\ncommon {passthrough}\n"
        ),
        _KUBECONFIG_KEY: yaml_config(kubeconfig(_CA_BUNDLE_PATH)),
    }
    tool_config = ConfigMap(
        scope,
        "sandbox-tool-config",
        metadata=ApiObjectMetadata(name=TOOL_CONFIG_MAP_NAME, namespace=env.namespace),
        data=tool_config_data,
    )
    Role(
        scope,
        "sandbox-tool-config-reader-role",
        metadata=ApiObjectMetadata(name=TOOL_CONFIG_READER_ROLE_NAME, namespace=env.namespace),
        rules=[RolePolicyRule(resources=[tool_config], verbs=["get"])],
    )


def tool_config_grant(namespace: str) -> RoleBindingGrant:
    return RoleBindingGrant(
        kind="RoleBinding", namespace=namespace, role_ref=RoleRef(kind="Role", name=TOOL_CONFIG_READER_ROLE_NAME)
    )


def workload_security_context() -> k8s.SecurityContext:
    return k8s.SecurityContext(allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"]))


def add_sidecar_settings(
    scope: Construct,
    env: Environment,
    *,
    name: str,
    platform_routes: Mapping[str, PlatformRoute],
    listen_address: str | None = None,
) -> SettingsFile:
    """The ConfigMap `name` holding the settings of the egress sidecar of one Pod template, which
    `pod_spec` mounts. Every Pod of a template gets the same file; nothing in it is per Pod."""
    content = {
        "proxy_host": egress.proxy(env.namespace).fqdn,
        "proxy_port": egress.PROXY_PORT,
        "readiness_listen_address": sidecar_config.READINESS_LISTEN_ADDRESS,
        **({"listen_address": listen_address} if listen_address is not None else {}),
        "token_file": f"{_EGRESS_TOKEN_DIR}/token",
        "audience_token_files": {audience: audience_token_file(audience) for audience in _SUBSTITUTABLE_AUDIENCE_FILES},
        **(
            {
                "platform_routes": {
                    route: spec.model_dump(mode="json", exclude_none=True) for route, spec in platform_routes.items()
                }
            }
            if platform_routes
            else {}
        ),
    }
    validate_settings_fragment(sidecar_config.Settings, content)
    return SettingsFile(
        scope,
        f"{name}-settings",
        metadata=ApiObjectMetadata(name=name, namespace=env.namespace),
        content=yaml_config(content),
        path=_SIDECAR_SETTINGS_PATH,
    )


def _egress_sidecar(
    env: Environment, settings: PodWiring, resources: k8s.ResourceRequirements, *, telemetry_enabled: bool
) -> k8s.Container:
    telemetry_env = (
        [
            k8s.EnvVar(name=name, value=value)
            for name, value in telemetry.service_environment(env.namespace, "agentplane-egress-sidecar").items()
        ]
        if telemetry_enabled
        else []
    )
    return k8s.Container(
        name=sidecar_config.CONTAINER_NAME,
        image=f"{_EGRESS_SIDECAR_IMAGE}:{_PLACEHOLDER_TAG}",
        env=[settings.path_env, *telemetry_env],
        security_context=workload_security_context(),
        readiness_probe=k8s.Probe(
            http_get=k8s.HttpGetAction(
                path=sidecar_config.READINESS_PATH, port=k8s.IntOrString.from_number(sidecar_config.READINESS_PORT)
            ),
            failure_threshold=1,
            period_seconds=2,
            timeout_seconds=1,
        ),
        resources=resources,
        volume_mounts=[
            k8s.VolumeMount(name=EGRESS_TOKEN_VOLUME_NAME, mount_path=_EGRESS_TOKEN_DIR, read_only=True),
            settings.mount,
        ],
    )


def _egress_volumes(env: Environment) -> list[k8s.Volume]:
    return [
        k8s.Volume(name=_EGRESS_CA_VOLUME_NAME, config_map=k8s.ConfigMapVolumeSource(name=env.egress.ca_secret_name)),
        k8s.Volume(name=_TOOL_CONFIG_VOLUME_NAME, config_map=k8s.ConfigMapVolumeSource(name=TOOL_CONFIG_MAP_NAME)),
        # The Pod's identity, and to nobody else: this volume is mounted by the egress sidecar
        # alone, so no token here is readable from the container an agent runs commands in.
        # `token` proves the Pod to the central proxy; each of the rest is the same account minted
        # for a destination's own audience, which the proxy substitutes where a rule names that
        # audience and which is useless at the proxy itself. All are bound to this Pod and rotated
        # by kubelet.
        k8s.Volume(
            name=EGRESS_TOKEN_VOLUME_NAME,
            projected=k8s.ProjectedVolumeSource(
                sources=[
                    k8s.VolumeProjection(
                        service_account_token=k8s.ServiceAccountTokenProjection(
                            audience=WORKLOAD_TOKEN_AUDIENCE, expiration_seconds=600, path="token"
                        )
                    ),
                    *(
                        k8s.VolumeProjection(
                            service_account_token=k8s.ServiceAccountTokenProjection(
                                audience=audience, expiration_seconds=600, path=file
                            )
                        )
                        for audience, file in _SUBSTITUTABLE_AUDIENCE_FILES.items()
                    ),
                ]
            ),
        ),
    ]


def pod_spec(
    env: Environment,
    *,
    workload: k8s.Container,
    additional_workloads: Sequence[k8s.Container] = (),
    sidecar_settings: SettingsFile,
    node_selector: Mapping[str, str] | None = None,
    workload_volumes: Sequence[k8s.Volume] = (),
    sidecar_resources: k8s.ResourceRequirements | None = None,
    image_pull_secret_name: str | None = SECRET_NAME,
    enable_telemetry: bool = False,
) -> k8s.PodSpec:
    """`workload` beside the egress sidecar, as uid 1000, with no ServiceAccount token in the Pod: the
    projected tokens are mounted by the sidecar alone, which is what keeps an account shared by
    several boxes out of the container a command runs in (agentplane/action_service/sandbox/README.md).
    A box names no account of its own: whoever creates one from this shape sets the identity it runs
    as (the Sandbox controller the Sandbox's own, the sandbox Actions the caller's).
    `additional_workloads` are other workload containers, before the egress sidecar. `sidecar_settings`
    is the sidecar's settings file (`add_sidecar_settings`), and `workload_volumes`
    are Pod volumes the workload mounts beyond the egress path's own. `node_selector` is the optional
    placement restriction for this template."""
    settings = sidecar_settings.pod_wiring(
        volume_name=SIDECAR_SETTINGS_VOLUME_NAME, env=sidecar_config.Settings.config_file_env
    )
    return k8s.PodSpec(
        # The workload first: with no default-container annotation, the first container is the one
        # `kubectl exec` and the sandbox Actions run a command in.
        containers=[
            workload,
            *additional_workloads,
            _egress_sidecar(
                env,
                settings,
                sidecar_resources
                or k8s.ResourceRequirements(
                    requests={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string("32Mi")},
                    limits={"cpu": k8s.Quantity.from_string("100m"), "memory": k8s.Quantity.from_string("128Mi")},
                ),
                telemetry_enabled=enable_telemetry,
            ),
        ],
        automount_service_account_token=False,
        # Container restarts create a fresh PID namespace for the isolated Codex executor.
        restart_policy="Always",
        image_pull_secrets=(
            [k8s.LocalObjectReference(name=image_pull_secret_name)] if image_pull_secret_name is not None else None
        ),
        node_selector=dict(node_selector) if node_selector is not None else None,
        # Do not let a runner or command process inspect another container's processes.
        share_process_namespace=False,
        termination_grace_period_seconds=60,
        security_context=k8s.PodSecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            run_as_group=1000,
            fs_group=1000,
            seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
        ),
        volumes=[*_egress_volumes(env), settings.volume, *workload_volumes],
    )


def render_pod_template(labels: Mapping[str, str], spec: k8s.PodSpec) -> dict[str, Any]:
    """`spec` under `labels`, as a PodTemplateSpec's Kubernetes JSON."""
    rendered: dict[str, Any] = k8s.KubePodTemplate(
        Chart(App(), "pod-template"),
        "podtemplate",
        template=k8s.PodTemplateSpec(metadata=k8s.ObjectMeta(labels=dict(labels)), spec=spec),
    ).to_json()["template"]
    return rendered
