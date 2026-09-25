"""The Pod shape every agentplane SandboxTemplate shares: the egress sidecar that relays a box's
traffic to the central proxy, the tokens only it mounts, the interception CA over the system bundle
and as Java's trust store, a system bazelrc that points Bazel at both, a kubeconfig that reaches the
API server through the proxy, and the proxy environment that points a workload at the sidecar. The
runner template (app.py) and the sandbox Actions' command box (command_sandbox.py) are both built
from it, so both kinds of box sit behind the same egress path.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplateSpecPodTemplateSpec,
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersEnv,
    SandboxTemplateSpecPodTemplateSpecContainersReadinessProbe,
    SandboxTemplateSpecPodTemplateSpecContainersReadinessProbeHttpGet,
    SandboxTemplateSpecPodTemplateSpecContainersReadinessProbeHttpGetPort,
    SandboxTemplateSpecPodTemplateSpecContainersResources,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests,
    SandboxTemplateSpecPodTemplateSpecContainersSecurityContext,
    SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities,
    SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
    SandboxTemplateSpecPodTemplateSpecImagePullSecrets,
    SandboxTemplateSpecPodTemplateSpecSecurityContext,
    SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile,
    SandboxTemplateSpecPodTemplateSpecVolumes,
    SandboxTemplateSpecPodTemplateSpecVolumesConfigMap,
    SandboxTemplateSpecPodTemplateSpecVolumesProjected,
    SandboxTemplateSpecPodTemplateSpecVolumesProjectedSources,
    SandboxTemplateSpecPodTemplateSpecVolumesProjectedSourcesServiceAccountToken,
)
from cdk8s_plus_34 import ConfigMap
from constructs import Construct

from agentplane.egress import sidecar
from agentplane.egress.resources import placeholder_of
from cluster.cdk8s.agentplane import egress, llm_ingress
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import SECRET_NAME
from cluster.cdk8s.metadata import metadata
from util.settings_contract import env_name

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_EGRESS_SIDECAR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"
# Mounted by the egress sidecar and no other container; every token under it is the Pod's own.
_EGRESS_TOKEN_DIR = "/var/run/agentplane-egress"
# Audiences the central proxy may substitute this Pod's identity for, and the file each is projected
# to under `_EGRESS_TOKEN_DIR`. The volume and the sidecar's mapping are both rendered from this, so
# neither can name a file the other does not project. The hop token is deliberately absent: it
# carries the proxy's own audience, so it is not substitutable anywhere.
_SUBSTITUTABLE_AUDIENCE_FILES = {egress.KUBERNETES_AUDIENCE: "kubernetes-token"}
_SIDECAR_LISTEN_PORT = 3128
# Shared by a workload's egress-ca volumeMount and the pod-level volume -- Kubernetes matches the
# two by this name.
_EGRESS_CA_VOLUME_NAME = "egress-ca"
_MITM_PROXY_URL = f"http://127.0.0.1:{_SIDECAR_LISTEN_PORT}"
_NO_PROXY_HOSTS = "127.0.0.1,localhost"
_CA_BUNDLE_PATH = "/etc/ssl/certs/ca-certificates.crt"
# Where Debian's JDKs keep the system trust store; Bazel's embedded JDK reads it only when told to.
_JAVA_TRUST_STORE_PATH = "/etc/ssl/certs/java/cacerts"
# Configuration files for the box's tools, one key each, mounted file by file.
_TOOL_CONFIG_MAP_NAME = "agentplane-sandbox-tool-config"
_TOOL_CONFIG_VOLUME_NAME = "tool-config"
_BAZELRC_KEY = "bazel.bazelrc"
_KUBECONFIG_KEY = "kubeconfig"
# Outside the home directory: a subPath mount creates its missing parents as root, and kubectl keeps
# its cache under ~/.kube.
_KUBECONFIG_PATH = "/etc/kubernetes/kubeconfig"
PROXY_VAR_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
NO_PROXY_VAR_NAMES = ("NO_PROXY", "no_proxy")
CA_BUNDLE_VAR_NAMES = ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "REQUESTS_CA_BUNDLE")


def egress_env() -> list[SandboxTemplateSpecPodTemplateSpecContainersEnv]:
    """The proxy environment, for the workload container itself.

    The box's fence lets it reach DNS and the egress proxy and nothing else, so a process that does
    not know to use the proxy has no egress at all -- and anything entering by another door
    (`kubectl exec`, the sandbox Actions' exec, a debug shell) is that kind of process. Both
    spellings, since clients disagree on case; NO_PROXY is loopback and nothing else. KUBECONFIG
    names the box's own identity at the API server, through the same proxy.
    """
    return [
        *(
            SandboxTemplateSpecPodTemplateSpecContainersEnv(name=name, value=_MITM_PROXY_URL)
            for name in PROXY_VAR_NAMES
        ),
        *(
            SandboxTemplateSpecPodTemplateSpecContainersEnv(name=name, value=_NO_PROXY_HOSTS)
            for name in NO_PROXY_VAR_NAMES
        ),
        *(
            SandboxTemplateSpecPodTemplateSpecContainersEnv(name=name, value=_CA_BUNDLE_PATH)
            for name in CA_BUNDLE_VAR_NAMES
        ),
        SandboxTemplateSpecPodTemplateSpecContainersEnv(name="KUBECONFIG", value=_KUBECONFIG_PATH),
    ]


def egress_mounts() -> list[SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts]:
    """Public roots + cluster root + the proxy's interception root, over the image's own bundle at the
    path every client falls back to and as Java's trust store, plus Bazel's system rc and the
    kubeconfig. A subPath mount does not follow ConfigMap updates: a CA rotation reaches a sandbox at
    its next Pod."""
    return [
        SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
            name=_EGRESS_CA_VOLUME_NAME, mount_path=_CA_BUNDLE_PATH, sub_path=egress.CA_BUNDLE_KEY, read_only=True
        ),
        SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
            name=_EGRESS_CA_VOLUME_NAME,
            mount_path=_JAVA_TRUST_STORE_PATH,
            sub_path=egress.JAVA_TRUST_STORE_KEY,
            read_only=True,
        ),
        SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
            name=_TOOL_CONFIG_VOLUME_NAME, mount_path="/etc/bazel.bazelrc", sub_path=_BAZELRC_KEY, read_only=True
        ),
        SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
            name=_TOOL_CONFIG_VOLUME_NAME, mount_path=_KUBECONFIG_PATH, sub_path=_KUBECONFIG_KEY, read_only=True
        ),
    ]


def add_tool_config(scope: Construct, env: Environment) -> None:
    """Bazel's system rc, which every Bazel in a box reads before its workspace's own, and the
    kubeconfig. Bazel's JVM fetches through the proxy but trusts only its own store, which lacks the
    interception root; and Bazel scrubs a test's environment, so the box's egress environment reaches
    a test only when named here. The kubeconfig reaches the API server through the proxy, verified
    against the bundle that carries the interception root, with the placeholder the proxy swaps for
    the box's own projected token: the box has no token of its own to put there; the sidecar holds
    it."""
    passthrough = " ".join(f"--test_env={var.name}" for var in egress_env())
    ConfigMap(
        scope,
        "sandbox-tool-config",
        metadata=metadata(_TOOL_CONFIG_MAP_NAME, env.namespace),
        data={
            _BAZELRC_KEY: (
                f"startup --host_jvm_args=-Djavax.net.ssl.trustStore={_JAVA_TRUST_STORE_PATH}\ncommon {passthrough}\n"
            ),
            _KUBECONFIG_KEY: yaml_config(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": [
                        {
                            "name": "in-cluster",
                            "cluster": {
                                "server": f"https://{egress.KUBERNETES_HOST}",
                                "certificate-authority": _CA_BUNDLE_PATH,
                            },
                        }
                    ],
                    "users": [{"name": "workload", "user": {"token": placeholder_of(egress.KUBERNETES_CREDENTIAL)}}],
                    "contexts": [{"name": "in-cluster", "context": {"cluster": "in-cluster", "user": "workload"}}],
                    "current-context": "in-cluster",
                }
            ),
        },
    )


def workload_security_context() -> SandboxTemplateSpecPodTemplateSpecContainersSecurityContext:
    return SandboxTemplateSpecPodTemplateSpecContainersSecurityContext(
        allow_privilege_escalation=False,
        capabilities=SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities(drop=["ALL"]),
    )


def _egress_sidecar(env: Environment) -> SandboxTemplateSpecPodTemplateSpecContainers:
    return SandboxTemplateSpecPodTemplateSpecContainers(
        name="egress-sidecar",
        image=f"{_EGRESS_SIDECAR_IMAGE}:{_PLACEHOLDER_TAG}",
        env=[
            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                name=env_name(sidecar.Settings, "proxy_host"),
                value=f"agentplane-egress.{env.namespace}.svc.cluster.local",
            ),
            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                name=env_name(sidecar.Settings, "proxy_port"), value=str(egress.PROXY_PORT)
            ),
            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                name=env_name(sidecar.Settings, "listen_port"), value=str(_SIDECAR_LISTEN_PORT)
            ),
            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                name=env_name(sidecar.Settings, "readiness_host"), value=sidecar.READINESS_HOST
            ),
            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                name=env_name(sidecar.Settings, "readiness_port"), value=str(sidecar.READINESS_PORT)
            ),
            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                name=env_name(sidecar.Settings, "token_file"), value=f"{_EGRESS_TOKEN_DIR}/token"
            ),
            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                name=env_name(sidecar.Settings, "audience_token_files"),
                value=json.dumps(
                    {
                        audience: f"{_EGRESS_TOKEN_DIR}/{file}"
                        for audience, file in _SUBSTITUTABLE_AUDIENCE_FILES.items()
                    }
                ),
            ),
        ],
        security_context=workload_security_context(),
        readiness_probe=SandboxTemplateSpecPodTemplateSpecContainersReadinessProbe(
            http_get=SandboxTemplateSpecPodTemplateSpecContainersReadinessProbeHttpGet(
                path=sidecar.READINESS_PATH,
                port=SandboxTemplateSpecPodTemplateSpecContainersReadinessProbeHttpGetPort.from_number(
                    sidecar.READINESS_PORT
                ),
            ),
            failure_threshold=1,
            period_seconds=2,
            timeout_seconds=1,
        ),
        resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
            requests={
                "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("10m"),
                "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("32Mi"),
            },
            limits={"memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("128Mi")},
        ),
        volume_mounts=[
            SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                name="egress-token", mount_path=_EGRESS_TOKEN_DIR, read_only=True
            )
        ],
    )


def _egress_volumes(env: Environment) -> list[SandboxTemplateSpecPodTemplateSpecVolumes]:
    return [
        SandboxTemplateSpecPodTemplateSpecVolumes(
            name=_EGRESS_CA_VOLUME_NAME,
            config_map=SandboxTemplateSpecPodTemplateSpecVolumesConfigMap(name=env.egress.ca_secret_name),
        ),
        SandboxTemplateSpecPodTemplateSpecVolumes(
            name=_TOOL_CONFIG_VOLUME_NAME,
            config_map=SandboxTemplateSpecPodTemplateSpecVolumesConfigMap(name=_TOOL_CONFIG_MAP_NAME),
        ),
        # The Pod's identity, and to nobody else: this volume is mounted by the egress sidecar
        # alone, so no token here is readable from the container an agent runs commands in.
        # `token` proves the Pod to the central proxy; each of the rest is the same account minted
        # for a destination's own audience, which the proxy substitutes where a rule names that
        # audience and which is useless at the proxy itself. All are bound to this Pod and rotated
        # by kubelet.
        SandboxTemplateSpecPodTemplateSpecVolumes(
            name="egress-token",
            projected=SandboxTemplateSpecPodTemplateSpecVolumesProjected(
                sources=[
                    SandboxTemplateSpecPodTemplateSpecVolumesProjectedSources(
                        service_account_token=SandboxTemplateSpecPodTemplateSpecVolumesProjectedSourcesServiceAccountToken(
                            audience=llm_ingress.WORKLOAD_TOKEN_AUDIENCE, expiration_seconds=600, path="token"
                        )
                    ),
                    *(
                        SandboxTemplateSpecPodTemplateSpecVolumesProjectedSources(
                            service_account_token=SandboxTemplateSpecPodTemplateSpecVolumesProjectedSourcesServiceAccountToken(
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
    workload: SandboxTemplateSpecPodTemplateSpecContainers,
    service_account_name: str | None,
    workload_volumes: Sequence[SandboxTemplateSpecPodTemplateSpecVolumes] = (),
) -> SandboxTemplateSpecPodTemplateSpec:
    """`workload` beside the egress sidecar, as uid 1000, with no ServiceAccount token in the Pod: the
    projected tokens are mounted by the sidecar alone, which is what keeps an account shared by
    several boxes out of the container a command runs in (agentplane/docs/sandbox_actions.md).
    `service_account_name` is the template's own; whoever stamps a Sandbox from it may replace it.
    `workload_volumes` are Pod volumes the workload mounts beyond the egress path's own."""
    return SandboxTemplateSpecPodTemplateSpec(
        containers=[workload, _egress_sidecar(env)],
        automount_service_account_token=False,
        image_pull_secrets=[SandboxTemplateSpecPodTemplateSpecImagePullSecrets(name=SECRET_NAME)],
        # With the rest of the namespace and with LiteLLM: a box's traffic through the egress proxy
        # and a runner's model calls both stay inside the zone.
        node_selector=(
            {"topology.kubernetes.io/zone": env.app.runner_zone} if env.app.runner_zone is not None else None
        ),
        service_account_name=service_account_name,
        termination_grace_period_seconds=60,
        security_context=SandboxTemplateSpecPodTemplateSpecSecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            run_as_group=1000,
            fs_group=1000,
            seccomp_profile=SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile(type="RuntimeDefault"),
        ),
        volumes=[*_egress_volumes(env), *workload_volumes],
    )
