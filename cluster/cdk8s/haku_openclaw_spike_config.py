"""The haku-openclaw-spike app: its namespace, openclaw.json (see model_rosters.py for
ANTHROPIC_MODELS), the gateway Deployment and everything around it.

The image tag is the placeholder "unset"; the hand-written `PINS_DIR` Component, which the
directory includes across the roots, overrides it at `kustomize build` time via Flux's
image-automation marker. The kubeconfig ConfigMap's payload is `kube-client-config` beside this
module, copied verbatim.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import haku_egress_proxy, namespaces, node_scheduling
from cluster.cdk8s.config_format import json5_config
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    GeneratorOptions,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import config_map_chart, copy_source_file
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.model_rosters import ANTHROPIC_MODELS
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.openclaw_gateway import (
    disabled_commands,
    haku_console_mcp,
    session_memory_hook,
    trusted_proxy_gateway,
)
from cluster.cdk8s.providers.seaweedfs.bucket import BucketAccess
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NAMESPACE = "haku-openclaw-spike"
_NAME = "haku-openclaw-spike"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/haku-openclaw-spike/app"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/agents/haku-openclaw-spike/app-image-pins"
# Authentik's outpost reaches it by FQDN (cluster/k8s/authentik/app/blueprints/haku-openclaw-spike-sso.yaml).
_GATEWAY = ServiceRef(
    name=_NAME,
    port=Port(name="gateway", number=18789),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
)
_CONFIG_MAP_NAME = "haku-openclaw-spike-config"
_KUBECONFIG_CONFIG_MAP_NAME = "haku-openclaw-spike-kubeconfig"
_KUBECONFIG_KEY = "config"
_GATEWAY_PASSWORD = SecretRef(namespace=_NAMESPACE, name="haku-openclaw-spike-gateway-password").key("password")
_STATE_CLAIM_NAME = "haku-openclaw-spike-state-v2"
_IMAGE = "git.allegedly.works/ducktape-ci/haku-openclaw-spike:unset"
_HOME = "/home/openclaw"
_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_NO_PROXY = "127.0.0.1,localhost"

# OpenClaw's own native `anthropic/<model>` id, not the `{provider}/{shape}/{model}`
# LiteLLM scheme -- this agent's `anthropic` plugin calls the Anthropic API directly,
# not through LiteLLM.
_MODEL_ALIASES = {
    "claude-opus-5": "Opus",
    "claude-sonnet-5": "Sonnet",
    "claude-fable-5": "Fable",
    "claude-haiku-4-5-20251001": "Haiku",
}


def _model_id(model: str) -> str:
    return f"anthropic/{model}"


def config() -> dict:
    return {
        "meta": {"migrations": {"modelPolicyAllowlist": True}},
        "agents": {
            "defaults": {
                "userTimezone": "America/Los_Angeles",
                "model": {"primary": _model_id(ANTHROPIC_MODELS[0])},
                "models": {
                    _model_id(model): {"agentRuntime": {"id": "claude-cli"}, "alias": _MODEL_ALIASES[model]}
                    for model in ANTHROPIC_MODELS
                },
                "modelPolicy": {"allow": [_model_id(model) for model in ANTHROPIC_MODELS]},
                "sandbox": {"mode": "off"},
                "skills": [],
                "verboseDefault": "full",
            },
            "entries": {"haku": {"default": True, "name": "Haku"}},
        },
        "memory": {
            "search": {
                # TODO: Enable semantic memory search after wiring an embedding provider.
                "enabled": False
            }
        },
        "commands": disabled_commands(),
        "cron": {"enabled": False},
        "gateway": trusted_proxy_gateway(
            allowed_origin="https://haku-openclaw-spike.allegedly.works", device_approve_scopes=["operator.admin"]
        ),
        "hooks": session_memory_hook(),
        "mcp": haku_console_mcp(request_timeout_ms=60000),
        "plugins": {"entries": {"anthropic": {"enabled": True}}},
        "tools": {
            "allow": [
                "exec",
                "process",
                "read",
                "write",
                "edit",
                "apply_patch",
                "memory_get",
                "memory_search",
                "session_status",
                "bundle-mcp",
            ],
            "elevated": {"enabled": False},
            "exec": {"mode": "full"},
            "fs": {"workspaceOnly": True},
            "sessions": {"visibility": "agent"},
        },
    }


def claude_config() -> dict:
    """claude.json -- OpenClaw's bundled Claude CLI runtime's own onboarding state, seeded
    so the CLI never prompts interactively inside the pod.
    """
    return {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "projects": {
            "/home/openclaw/.openclaw/workspace": {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
            }
        },
    }


def namespace_chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=_NAMESPACE,
        vpa=Vpa.AUTO,
        agent_readable=None,
        labels={"name": _NAMESPACE},
        annotations={"description": "Isolated OpenClaw plus Claude Code subscription compatibility spike for Haku."},
    )
    return chart


def chart(app: App) -> Chart:
    return config_map_chart(
        app,
        chart_name="haku-openclaw-spike-config",
        configmap_name=_CONFIG_MAP_NAME,
        namespace=_NAMESPACE,
        data={"openclaw.json": json5_config(config()), "claude.json": json5_config(claude_config())},
    )


_SEED_STATE_SCRIPT = textwrap.dedent(
    r"""
    set -eu
    mkdir -p /home/openclaw/.openclaw /home/openclaw/.cache/npm /home/openclaw/.local
    cp /cfg/openclaw.json /home/openclaw/.openclaw/openclaw.json
    rm -f /home/openclaw/.openclaw/openclaw.json.last-good /home/openclaw/.openclaw/openclaw.json.bak*
    if [ ! -e /home/openclaw/.claude.json ]; then
      cp /cfg/claude.json /home/openclaw/.claude.json
    fi

    git config --global user.name haku
    git config --global user.email haku@allegedly.works
    umask 077
    cat > /home/openclaw/.netrc <<NETRC
    machine forgejo-http.forgejo
    login ${HAKU_GIT_USERNAME}
    password ${HAKU_GIT_PASSWORD}
    machine github.com
    login x-access-token
    password ${GH_PAT}
    NETRC

    # Bazel's downloader uses the JVM trust store rather than the
    # mounted system PEM bundle. Import the interception root into a
    # small workspace-owned store and point the Bazel server at it.
    truststore=/home/openclaw/egress-truststore.p12
    rm -f "$truststore"
    tmp="$(mktemp -d)"
    csplit -sz -f "$tmp/c" -b '%02d.pem' /etc/ssl/certs/ca-certificates.crt \
      '/-----BEGIN CERTIFICATE-----/' '{*}'
    for cert in "$tmp"/c*.pem; do
      if openssl x509 -noout -subject -in "$cert" 2>/dev/null \
        | grep -q haku-egress-proxy-root-ca; then
        keytool -importcert -noprompt -storepass changeit -storetype PKCS12 \
          -keystore "$truststore" -alias haku-egress-proxy -file "$cert"
      fi
    done
    rm -rf "$tmp"
    #
    # rules_python shells out to a pip that trusts only its vendored
    # certifi, so the container also exports PIP_CERT and friends.
    # Repository rules inherit the pod environment anyway, but an
    # undeclared variable never enters a repo's marker file, so a
    # repository cached from a pre-fix TLS failure would keep serving
    # that failure; --repo_env ties the cached fetch to the trust
    # settings and refetches when they change.
    cat > /home/openclaw/.bazelrc <<BAZELRC
    startup --host_jvm_args=-Djavax.net.ssl.trustStore=$truststore
    startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit
    startup --host_jvm_args=-Djavax.net.ssl.trustStoreType=PKCS12
    # Keep Bazel's output base + disk cache on the ephemeral /bazel-cache
    # emptyDir, never the persistent state PVC: a single agent run accreted
    # a 65G output base of regenerable build artifacts on the PVC before this
    # migration. Ephemeral means re-fetched per pod start, and the volume's
    # sizeLimit caps runaway growth (the enforcement gap in #4717).
    startup --output_user_root=/bazel-cache/output
    build --disk_cache=/bazel-cache/disk
    build --repo_env=SSL_CERT_FILE
    build --repo_env=REQUESTS_CA_BUNDLE
    build --repo_env=PIP_CERT
    BAZELRC
    """
).lstrip("\n")


def _env(name: str, value: str) -> k8s.EnvVar:
    return k8s.EnvVar(name=name, value=value)


# All non-secret placeholders. The dedicated iron-proxy replaces them only in matching
# Authorization headers for their exact destination hosts.
_CLAUDE_CODE_OAUTH_TOKEN = _env("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-proxy-haku-openclaw-placeholder")
_HAKU_GIT_USERNAME = _env("HAKU_GIT_USERNAME", "haku")
_HAKU_GIT_PASSWORD = _env("HAKU_GIT_PASSWORD", "proxy-haku-forgejo-placeholder")
# Deliberately not GITHUB_TOKEN or GH_TOKEN: OpenClaw's generic exec environment filter
# treats those exact names as credentials, with a current local-Gateway exception for
# native GitHub identity. iron-proxy replaces this one only in GitHub Authorization headers.
_GH_PAT = _env("GH_PAT", "proxy-github-placeholder")

_CONTAINER_SECURITY_CONTEXT = k8s.SecurityContext(
    allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
)


def _mount(
    name: str, mount_path: str, *, sub_path: str | None = None, read_only: bool | None = None
) -> k8s.VolumeMount:
    return k8s.VolumeMount(name=name, mount_path=mount_path, sub_path=sub_path, read_only=read_only)


_CONFIG_MOUNT = _mount("config", "/cfg", read_only=True)
# The complete home is persistent: OpenClaw state/workspace plus Claude Code's native
# transcripts and resumable session metadata.
_STATE_MOUNT = _mount("state", _HOME)
_TRUST_MOUNT = _mount("trust", _CA_BUNDLE, sub_path="ca-certificates.crt", read_only=True)
_TMP_MOUNT = _mount("tmp", "/tmp")


def _seed_state_container() -> k8s.Container:
    return k8s.Container(
        name="seed-state",
        image=_IMAGE,
        command=["sh", "-c", _SEED_STATE_SCRIPT],
        env=[_env("HOME", _HOME), _CLAUDE_CODE_OAUTH_TOKEN, _HAKU_GIT_USERNAME, _HAKU_GIT_PASSWORD, _GH_PAT],
        security_context=_CONTAINER_SECURITY_CONTEXT,
        volume_mounts=[_CONFIG_MOUNT, _STATE_MOUNT, _TRUST_MOUNT, _TMP_MOUNT],
    )


def _openclaw_container() -> k8s.Container:
    return k8s.Container(
        name="openclaw",
        image=_IMAGE,
        env=[
            _env("HOME", _HOME),
            _env("NPM_CONFIG_PREFIX", f"{_HOME}/.local"),
            _env("NPM_CONFIG_CACHE", f"{_HOME}/.cache/npm"),
            _env("OPENCLAW_LOG_LEVEL", "debug"),
            # Authentik authenticates proxied browser traffic. Local backend clients have
            # no proxy identity headers, so use OpenClaw's documented trusted-proxy
            # local-password fallback.
            _GATEWAY_PASSWORD.env_var("OPENCLAW_GATEWAY_PASSWORD"),
            _CLAUDE_CODE_OAUTH_TOKEN,
            # OpenClaw clears ambient Claude credentials by default so auth profiles cannot
            # accidentally inherit host credentials. This deployment intentionally supplies
            # its proxy-mediated OAuth token through the environment, so preserve that one
            # variable for the managed Claude CLI child process.
            _env("OPENCLAW_LIVE_CLI_BACKEND_PRESERVE_ENV", "CLAUDE_CODE_OAUTH_TOKEN"),
            _env("HAKU_CONSOLE_TOKEN", "proxy-haku-console-placeholder"),
            _HAKU_GIT_USERNAME,
            _HAKU_GIT_PASSWORD,
            _GH_PAT,
            _env("HAKU_STATE_REPO_URL", "http://forgejo-http.forgejo:3000/haku/haku-state.git"),
            _env("NODE_USE_ENV_PROXY", "1"),
            # Keep both cases: Node honors the uppercase variables, while curl/libcurl (and
            # therefore Git over plain HTTP) require the lowercase form and intentionally
            # ignore uppercase HTTP_PROXY.
            _env("HTTP_PROXY", haku_egress_proxy.OPENCLAW_SPIKE_PROXY.url),
            _env("HTTPS_PROXY", haku_egress_proxy.OPENCLAW_SPIKE_PROXY.url),
            _env("NO_PROXY", _NO_PROXY),
            _env("http_proxy", haku_egress_proxy.OPENCLAW_SPIKE_PROXY.url),
            _env("https_proxy", haku_egress_proxy.OPENCLAW_SPIKE_PROXY.url),
            _env("no_proxy", _NO_PROXY),
            _env("NODE_EXTRA_CA_CERTS", _CA_BUNDLE),
            # Python is the other runtime that ignores the mounted system bundle: pip trusts
            # only its vendored certifi, and the hermetic interpreter rules_python downloads
            # carries its own OpenSSL whose compiled-in CA path is not Debian's. Without
            # these, every pip fetch fails TLS against the egress proxy even though pypi.org
            # and files.pythonhosted.org are allowlisted -- which reads as "no route to PyPI"
            # rather than as a trust fault.
            _env("SSL_CERT_FILE", _CA_BUNDLE),
            _env("REQUESTS_CA_BUNDLE", _CA_BUNDLE),
            _env("PIP_CERT", _CA_BUNDLE),
        ],
        ports=[_GATEWAY.port.k8s_container_port()],
        # Without this the Deployment reports 1/1 Running whenever a process exists, which
        # hid two different outages during the 2026.8.1 recovery: a gateway crash-looping
        # every ~4 minutes, and one that started but never bound its port. /healthz answers
        # 200 unauthenticated on a serving gateway (/readyz and / are auth-gated at 403), so
        # it checks that the HTTP server is really handling requests.
        #
        # Readiness only, deliberately no liveness or startup probe: startup is IO-bound on
        # the state PVC and legitimately takes many minutes -- a gateway on the OVH HDD was
        # measured reading ~940 KB/s, hundreds of MB in, still progressing. A restarting
        # probe would kill that and never let it finish.
        readiness_probe=k8s.Probe(
            http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string(_GATEWAY.port.name)),
            initial_delay_seconds=10,
            period_seconds=10,
            timeout_seconds=5,
            failure_threshold=3,
        ),
        security_context=_CONTAINER_SECURITY_CONTEXT,
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("1Gi")},
            # Provisional high memory limit after an earlier VPA memory observation;
            # investigate resident-memory growth before treating it as normal.
            limits={"cpu": k8s.Quantity.from_string("4"), "memory": k8s.Quantity.from_string("16Gi")},
        ),
        volume_mounts=[
            _CONFIG_MOUNT,
            # Read-only kubeconfig at ~/.kube/config, where kubectl finds it by default.
            # Its token is a placeholder iron-proxy swaps for the real Authentik JWT for
            # kubeapi.allegedly.works only; with no service account token mounted, this
            # path grants exactly what RBAC binds to oidc-ksbx-groups:haku. subPath because
            # the state volume owns /home/openclaw.
            _mount("kubeconfig", f"{_HOME}/.kube/config", sub_path=_KUBECONFIG_KEY, read_only=True),
            _STATE_MOUNT,
            # Ephemeral Bazel output base + disk cache (targeted by the init bazelrc), kept
            # off the persistent state volume.
            _mount("bazel-cache", "/bazel-cache"),
            _TRUST_MOUNT,
            _TMP_MOUNT,
        ],
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels=_GATEWAY.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_GATEWAY.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_GATEWAY.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    # State is on the worker-local state-v2 PVC. Exclude control-plane nodes
                    # so the agent cannot move its I/O back onto the etcd/kubelet hosts.
                    affinity=k8s.Affinity(
                        node_affinity=k8s.NodeAffinity(
                            required_during_scheduling_ignored_during_execution=k8s.NodeSelector(
                                node_selector_terms=[
                                    k8s.NodeSelectorTerm(
                                        match_expressions=[
                                            k8s.NodeSelectorRequirement(
                                                key=node_scheduling.CONTROL_PLANE_TAINT_KEY, operator="DoesNotExist"
                                            )
                                        ]
                                    )
                                ]
                            )
                        )
                    ),
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        fs_group=1000,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    init_containers=[_seed_state_container()],
                    containers=[_openclaw_container()],
                    node_selector={"topology.kubernetes.io/region": "home"},
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP_NAME)),
                        k8s.Volume(
                            name="kubeconfig", config_map=k8s.ConfigMapVolumeSource(name=_KUBECONFIG_CONFIG_MAP_NAME)
                        ),
                        k8s.Volume(
                            name="state",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_STATE_CLAIM_NAME),
                        ),
                        k8s.Volume(
                            name="trust", config_map=k8s.ConfigMapVolumeSource(name="haku-egress-proxy-ca-cert")
                        ),
                        k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                        # Ephemeral, capped Bazel output base + disk cache -- deliberately NOT
                        # the state PVC and NOT local-path (which enforces no size, #4717).
                        # sizeLimit bounds the regenerable cache so a runaway build evicts the
                        # pod rather than filling the node disk.
                        k8s.Volume(
                            name="bazel-cache",
                            empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("30Gi")),
                        ),
                    ],
                ),
            ),
        ),
    )


def _state_claim(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "state-v2",
        metadata=k8s.ObjectMeta(
            name=_STATE_CLAIM_NAME,
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "Persistent OpenClaw home, agent workspace, memory, and Claude Code session "
                    "transcripts for the isolated Haku spike, on the optiplex worker's local SSD, "
                    "away from the control-plane disks etcd and kubelet need."
                )
            },
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="local-path-home-ssd",
            # local-path does not enforce PVC requests as quotas; this request leaves
            # headroom for state growth on the worker's shared local storage.
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("40Gi")}),
        ),
    )


def _gateway_password(scope: Construct) -> None:
    """Authentik authenticates browser traffic through the trusted proxy. OpenClaw backend
    clients, including subagent completion handoffs, connect directly to the loopback
    Gateway and therefore have no Authentik identity headers; OpenClaw's local password
    fallback covers them. Generated once and retained, injected only into this workload."""
    mint_bearer_secret(
        scope,
        "gateway-password",
        name=_GATEWAY_PASSWORD.secret.name,
        namespace=_GATEWAY_PASSWORD.secret.namespace,
        key=_GATEWAY_PASSWORD.key,
        generator_name=f"{_GATEWAY_PASSWORD.secret.name}-generator",
        # The generator value is stable. Avoid automatic rotation, which would
        # interrupt active local Gateway clients unnecessarily.
        refresh="8760h",
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        secret_type=None,
    )


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=_GATEWAY.name, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(selector=_GATEWAY.pods.selector, ports=[_GATEWAY.port.k8s_service_port()]),
    )


def _network_policies(scope: Construct) -> None:
    # Trusted-proxy authentication is safe only when the Authentik server is the sole
    # workload allowed to supply identity headers.
    k8s.KubeNetworkPolicy(
        scope,
        "ingress",
        metadata=k8s.ObjectMeta(name="haku-openclaw-spike-ingress", namespace=_NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_GATEWAY.pods.selector),
            policy_types=["Ingress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "authentik"}
                            ),
                            pod_selector=k8s.LabelSelector(
                                match_labels={
                                    "app.kubernetes.io/component": "server",
                                    "app.kubernetes.io/instance": "authentik",
                                }
                            ),
                        )
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_GATEWAY.pod_port), protocol="TCP")],
                )
            ],
        ),
    )
    # The app cannot bypass credential substitution: every non-loopback network request
    # must traverse the dedicated iron-proxy. DNS is the only other egress.
    k8s.KubeNetworkPolicy(
        scope,
        "egress",
        metadata=k8s.ObjectMeta(name="haku-openclaw-spike-egress", namespace=_NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_GATEWAY.pods.selector),
            policy_types=["Egress"],
            egress=[
                k8s.NetworkPolicyEgressRule(
                    to=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "kube-system"}
                            ),
                            pod_selector=k8s.LabelSelector(match_labels={"k8s-app": "kube-dns"}),
                        )
                    ],
                    ports=[
                        k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(53), protocol="UDP"),
                        k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(53), protocol="TCP"),
                    ],
                ),
                k8s.NetworkPolicyEgressRule(
                    to=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={
                                    "kubernetes.io/metadata.name": haku_egress_proxy.OPENCLAW_SPIKE_PROXY.pods.namespace
                                }
                            ),
                            pod_selector=k8s.LabelSelector(
                                match_labels=haku_egress_proxy.OPENCLAW_SPIKE_PROXY.pods.selector
                            ),
                        )
                    ],
                    ports=[
                        k8s.NetworkPolicyPort(
                            port=k8s.IntOrString.from_number(haku_egress_proxy.OPENCLAW_SPIKE_PROXY.pod_port),
                            protocol="TCP",
                        )
                    ],
                ),
            ],
        ),
    )


def _backup_bucket(scope: Construct) -> None:
    """The VolSync backup bucket and the credentials Secret ../backup's SecretStore reads."""
    # No S3Identity declares this IAM identity; S3Credentials uses the existing one by name.
    identity = "haku-openclaw-spike-backups"
    # Restic retention/pruning is managed by VolSync, not by Bucket deletion.
    s3.bucket(
        scope,
        "backup-bucket",
        name="haku-openclaw-spike-backups",
        namespace=_NAMESPACE,
        access=[BucketAccess.read_write(identity)],
        adopt_existing=True,
        description="Haku OpenClaw spike VolSync backup bucket.",
    )
    s3.credentials(
        scope,
        "backup-credentials",
        identity=identity,
        namespace=_NAMESPACE,
        # Generated directly where the VolSync SecretStore reads it.
        secret="haku-openclaw-spike-volsync-s3-credentials",
        key_fields=s3.AWS_ENV_KEY_FIELDS,
        description="Haku OpenClaw spike VolSync SeaweedFS credentials.",
    )


def app_chart(app: App) -> Chart:
    """The gateway workload, its credentials, storage, network policy and backup bucket."""
    workload = Chart(app, _NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(workload, "forgejo-images-creds", namespace=_NAMESPACE)
    _gateway_password(workload)
    _state_claim(workload)
    _deployment(workload)
    _service(workload)
    _network_policies(workload)
    _backup_bucket(workload)
    return workload


GENERATOR_OPTIONS = GeneratorOptions(disable_name_suffix_hash=True)


def write_kubeconfig_config_map(root: Path) -> ConfigMapArgs:
    """Copy `kube-client-config` into `OUTPUT_DIR`; return the `configMapGenerator` entry packaging
    it under the key the Deployment mounts at ~/.kube/config."""
    return ConfigMapArgs(
        name=_KUBECONFIG_CONFIG_MAP_NAME,
        namespace=_NAMESPACE,
        files=[f"{_KUBECONFIG_KEY}=" + copy_source_file(root, OUTPUT_DIR, "cluster/cdk8s/kube-client-config")],
    )


def haku_openclaw_spike_app(
    chart: Chart,
    directory: RenderedDirectory,
    external_secrets_operator: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "haku-openclaw-spike-app",
        directory,
        timeout="10m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator, seaweedfs_operator),
        description=(
            "Isolated OpenClaw gateway using Claude Code subscription inference through the Haku credential proxy."
        ),
    )
