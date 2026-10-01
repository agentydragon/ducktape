"""haku-egress-proxy (cluster/k8s/agents/haku-egress-proxy): the mitmproxy egress chokepoint
for haku-sandbox and haku-ci, its interception CA and trust bundle, and the OpenClaw spike's
iron-proxy credential substituter.

Its generated file also holds the Namespace from agents/namespaces.py and the
CiliumNetworkPolicies from egress_fences.py. Hand-written there:
`kustomization.yaml` (a patch renames a SOPS Secret), the SOPS Secrets, and
`image-pins/kustomization.yaml`, which overrides the iron-proxy placeholder tag via Flux's
image-automation marker.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s import cilium, egress_fences, external_creds
from cluster.cdk8s.agents import namespaces
from cluster.cdk8s.cert_manager.interception_ca import interception_root_ca
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "haku-egress-proxy"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/haku-egress-proxy"
# The mitmproxy chokepoint haku-sandbox and haku-ci send their external egress through.
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="proxy", number=8080),
    pods=Pods(namespace=NAME, labels=(("app.kubernetes.io/name", NAME),)),
)
_CA_SECRET = "haku-egress-proxy-ca"
_IRON_PROXY_IMAGE = "git.allegedly.works/ducktape-ci/iron-proxy:unset"
_IRON_PROXY_METRICS = Port(name="metrics", number=9090)
# The OpenClaw spike's only egress path.
OPENCLAW_SPIKE_PROXY = ServiceRef(
    name="haku-openclaw-spike-proxy",
    port=Port(name="proxy", number=8181),
    pods=Pods(namespace=NAME, labels=(("app.kubernetes.io/name", "haku-openclaw-spike-proxy"),)),
)
_PUBLISHED_SECRETS_READER = "authentik-jwt-rotation-published-secrets-reader"


def _quantities(**values: str) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _ca(chart: Chart) -> None:
    interception_root_ca(
        chart,
        name="haku-egress-proxy-root-ca",
        namespace=NAME,
        secret_name=_CA_SECRET,
        bundle_name="haku-egress-proxy-ca-cert",
        description="Trust bundle for haku-egress-proxy-inspected sandbox HTTPS traffic",
        reflection_namespaces=("cert-manager",),
        # Written into Haku trust domains. The CLIProxyAPI-backed aiquota path connects
        # directly to the in-cluster management service and does not trust or use this
        # inspected egress listener. public-coder-agent has its own separate interception CA
        # (public_coder_proxy.py) and does not consume this bundle.
        target_namespaces=("haku-sandbox", "haku-openclaw-spike", "haku-ci"),
    )


_MITMPROXY_CA_INIT_SCRIPT = """\
cat /mitmproxy-ca/tls.key /mitmproxy-ca/tls.crt > /mitmproxy-data/mitmproxy-ca.pem
cp /mitmproxy-ca/tls.crt /mitmproxy-data/mitmproxy-ca-cert.pem
"""


def _mitmproxy(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME, labels=SERVICE.pods.selector),
        spec=k8s.DeploymentSpec(
            # Two, so one container's restart never empties the Service. mitmproxy OOM-kills
            # under haku-ci traffic (#5846), and with one replica every kill was a CI outage:
            # dependency fetches mid-flight got "connection refused" for the restart's duration.
            replicas=2,
            selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    affinity=k8s.Affinity(
                        pod_anti_affinity=k8s.PodAntiAffinity(
                            # Preferred, not required: this proxy is haku-ci's only egress path, so a
                            # replica left Pending by a hard rule costs more than two on one node.
                            preferred_during_scheduling_ignored_during_execution=[
                                k8s.WeightedPodAffinityTerm(
                                    weight=100,
                                    pod_affinity_term=k8s.PodAffinityTerm(
                                        label_selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
                                        topology_key="kubernetes.io/hostname",
                                    ),
                                )
                            ]
                        )
                    ),
                    init_containers=[
                        k8s.Container(
                            name="mitmproxy-ca-init",
                            image="busybox:1.38",
                            command=["sh", "-c", _MITMPROXY_CA_INIT_SCRIPT],
                            volume_mounts=[
                                k8s.VolumeMount(name="mitmproxy-ca", mount_path="/mitmproxy-ca", read_only=True),
                                k8s.VolumeMount(name="mitmproxy-data", mount_path="/mitmproxy-data"),
                            ],
                        )
                    ],
                    containers=[
                        k8s.Container(
                            name="mitmproxy",
                            # Egress proxy — currently implemented with mitmproxy.
                            # >=12.2.3 (mitmproxy#8214): the leaf's AuthorityKeyIdentifier now
                            # copies the CA cert's SubjectKeyIdentifier instead of recomputing it
                            # as SHA-1. cert-manager mints CA SKIs per RFC 7093 (truncated
                            # SHA-256), so on <12.2.3 every intercepted leaf's AKID mismatched the
                            # CA SKI and strict clients rejected the chain ("unable to get local
                            # issuer certificate"). See
                            # cluster/docs/lessons_learned/2026_06_25_mitmproxy_ca_ski_aki_mismatch.md.
                            image="mitmproxy/mitmproxy:12.2.3",
                            command=[
                                # mitmdump, not mitmweb. mitmweb keeps every flow in its View store
                                # for the UI, with no eviction, so under CI traffic the store grew
                                # until the container was OOM-killed (#5846). mitmdump keeps no
                                # view: it logs one line per flow to stdout and retains nothing.
                                # Nothing used the 8081 UI.
                                "mitmdump",
                                "--listen-host",
                                "0.0.0.0",
                                "--listen-port",
                                str(SERVICE.pod_port),
                                "--set",
                                "confdir=/mitmproxy-data",
                                # Stream (don't buffer) response bodies over 1 MB. dind pulls
                                # multi-hundred-MB image layers through here; buffering them
                                # OOM-killed the container (exit 137, connection refused
                                # mid-restart → haku-ci builds failed). We gate at the
                                # CONNECT/host level (the allowlist), not blob content, so
                                # streaming loses no enforcement.
                                "--set",
                                "stream_large_bodies=1m",
                                # Passthrough (raw TCP, no TLS interception) for Anthropic's control
                                # plane. The haku-managed-agent worker drives Managed Agents sessions
                                # over a long-lived HTTP/2 stream to api.anthropic.com; mitmproxy's
                                # interception buffers/breaks that stream, so the worker claims work
                                # but tool results never post and sessions deadlock at "idle". This
                                # traffic carries only the scoped environment key and needs no
                                # inspection. Egress still flows through the proxy (the CCNP
                                # chokepoint is unchanged); only TLS interception is skipped here.
                                # TODO(haku): tighten later — this passes ALL of api.anthropic.com
                                #   through untouched. Revisit whether only the session stream needs
                                #   passthrough (and whether to inspect the rest).
                                "--ignore-hosts",
                                r"api\.anthropic\.com",
                            ],
                            ports=[SERVICE.port.k8s_container_port()],
                            # An endpoint only once the listener is up: with two replicas a rolling
                            # update otherwise routes to a pod that is not listening yet.
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string(SERVICE.port.name)),
                                period_seconds=5,
                            ),
                            volume_mounts=[k8s.VolumeMount(name="mitmproxy-data", mount_path="/mitmproxy-data")],
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="50m", memory="256Mi"),
                                # Memory: headroom over the 512Mi that OOM-killed under haku-ci
                                # build traffic.
                                # TODO(vpa-memory-audit): 1Gi -> 3Gi. VPA observed a 1.15Gi
                                # request / 1.73Gi upper bound, and mitmproxy was OOM-killed at 3Gi
                                # again on 2026-09-08 after ~53h of traffic (#5846). The cause was
                                # mitmweb's flow store, gone since the switch to mitmdump; observe
                                # the working set under mitmdump and lower this to match.
                                limits=_quantities(cpu="500m", memory="3Gi"),
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="mitmproxy-ca", secret=k8s.SecretVolumeSource(secret_name=_CA_SECRET)),
                        k8s.Volume(name="mitmproxy-data", empty_dir=k8s.EmptyDirVolumeSource()),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAME),
        spec=k8s.ServiceSpec(selector=SERVICE.pods.selector, ports=[SERVICE.port.k8s_service_port()]),
    )
    # With two replicas, a voluntary disruption (node drain, descheduler eviction, rolling
    # update) may take one proxy pod at a time but never both, so haku-ci's only egress path
    # keeps a ready endpoint throughout. The descheduler honors PDBs unconditionally.
    k8s.KubePodDisruptionBudget(
        chart,
        "poddisruptionbudget",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(1), selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector)
        ),
    )
    # Allow proxy clients (haku-sandbox + haku-ci) to reach the egress proxy (port 8080) and
    # the Authentik outpost to reach the mitmweb UI (port 8081) for proxy auth. Without the
    # haku-ci ingress the runner/dind get `proxyconnect ... i/o timeout` on every egress.
    k8s.KubeNetworkPolicy(
        chart,
        "networkpolicy",
        metadata=k8s.ObjectMeta(name="allow-authentik-egress-proxy-ingress", namespace=NAME),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": namespace}
                            )
                        )
                        for namespace in ("haku-sandbox", "haku-ci")
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(SERVICE.pod_port), protocol="TCP")],
                ),
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "authentik"}
                            )
                        )
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(8081), protocol="TCP")],
                ),
            ],
            policy_types=["Ingress"],
        ),
    )


def _iron_proxy(chart: Chart, service: ServiceRef, *, description: str, config: dict, env: list[k8s.EnvVar]) -> None:
    """An iron-proxy Deployment holding real credentials and substituting them for a sandbox's
    placeholders, its config, and its Service."""
    name = service.name
    labels = service.pods.selector
    # No content-hash name suffix: Reloader's `autoReloadAll` rolls the proxy when this changes.
    config_map = k8s.KubeConfigMap(
        chart,
        f"{name}-config",
        metadata=k8s.ObjectMeta(name=f"{name}-config", namespace=NAME),
        data={"iron.yaml": yaml_config(config)},
    )
    k8s.KubeDeployment(
        chart,
        f"{name}-deployment",
        metadata=k8s.ObjectMeta(name=name, namespace=NAME, labels=labels, annotations={"description": description}),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=labels),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65532,
                        run_as_group=65532,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="iron-proxy",
                            image=_IRON_PROXY_IMAGE,
                            args=["-config", "/etc/iron-proxy/iron.yaml"],
                            env=env,
                            ports=[service.port.k8s_container_port(), _IRON_PROXY_METRICS.k8s_container_port()],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="50m", memory="128Mi"),
                                limits=_quantities(cpu="500m", memory="512Mi"),
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/etc/iron-proxy", read_only=True),
                                k8s.VolumeMount(name="ca", mount_path="/ca", read_only=True),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=config_map.name)),
                        k8s.Volume(name="ca", secret=k8s.SecretVolumeSource(secret_name=_CA_SECRET)),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        f"{name}-service",
        metadata=k8s.ObjectMeta(name=name, namespace=NAME),
        spec=k8s.ServiceSpec(
            selector=labels, ports=[service.port.k8s_service_port(), _IRON_PROXY_METRICS.k8s_service_port()]
        ),
    )


def _github_token(chart: Chart, token: SecretKey) -> None:
    """The agentydragon-agent GitHub PAT, consumed only by one iron-proxy here; its sandbox
    receives a non-secret placeholder that the proxy replaces in Authorization headers for
    exact GitHub hosts."""
    ExternalSecret(
        chart,
        token.secret.name,
        metadata=ApiObjectMetadata(name=token.secret.name, namespace=token.secret.namespace),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data("github-agentydragon-agent", "token", secret_key=token.key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def _substitution(env: str, placeholder: str, *hosts: str) -> dict:
    """An iron `secrets` entry: the value of `env` replaces `placeholder` in Authorization, on
    `hosts` only. iron-proxy also substitutes inside base64 Basic authorization, which is how
    git over HTTP carries a password."""
    return {
        "source": {"type": "env", "var": env},
        "replace": {"proxy_value": placeholder, "match_headers": ["Authorization"]},
        "rules": [{"host": host} for host in hosts],
    }


def _openclaw_spike_iron_config() -> dict:
    return {
        "dns": {"enabled": False},
        "proxy": {
            "tunnel_listen": f":{OPENCLAW_SPIKE_PROXY.pod_port}",
            # Forgejo generates a full-history Git pack before returning response headers. The
            # default 30s cap aborts that request with HTTP 502, while shallow fetches finish in
            # time. Keep a bounded but practical limit.
            "upstream_response_header_timeout": "5m",
        },
        "tls": {"mode": "mitm", "ca_cert": "/ca/tls.crt", "ca_key": "/ca/tls.key"},
        "transforms": [
            # The L7 bound; the same tuple is the DNS half of the proxy's Cilium fence.
            {"name": "allowlist", "config": {"domains": list(egress_fences.OPENCLAW_SPIKE_ALLOWLIST)}},
            {
                "name": "secrets",
                "config": {
                    "secrets": [
                        _substitution(
                            "CLAUDE_CODE_OAUTH_TOKEN",
                            "sk-ant-oat01-proxy-haku-openclaw-placeholder",
                            "api.anthropic.com",
                        ),
                        _substitution("HAKU_GIT_PASSWORD", "proxy-haku-forgejo-placeholder", "forgejo-http.forgejo"),
                        _substitution("HAKU_CONSOLE_TOKEN", "proxy-haku-console-placeholder", "haku.allegedly.works"),
                        _substitution(
                            "GITHUB_TOKEN",
                            "proxy-github-placeholder",
                            "api.github.com",
                            "github.com",
                            "codeload.github.com",
                        ),
                        # An Authentik client_credentials JWT for the `haku` k8s group, minted and
                        # rotated by agents/authentik-jwt-rotation. The apiserver derives
                        # oidc-ksbx-groups:haku from it, so what this runtime may do in the
                        # cluster is RBAC on that group -- this proxy only delivers the bearer,
                        # it cannot tell `get pods` from `delete ns`.
                        _substitution("HAKU_KUBE_JWT", "proxy-haku-kube-placeholder", "kubeapi.allegedly.works"),
                    ]
                },
            },
        ],
        "log": {"level": "info"},
    }


def _openclaw_spike_proxy(chart: Chart) -> None:
    # Shared with public-coder-agent's copy of the same PAT.
    github_token = SecretRef(namespace=NAME, name="haku-openclaw-spike-github-token").key("GITHUB_TOKEN")
    kube_token = SecretRef(namespace=NAME, name="haku-openclaw-spike-kube-token")
    _github_token(chart, github_token)
    _iron_proxy(
        chart,
        OPENCLAW_SPIKE_PROXY,
        description=(
            "Holds Haku OpenClaw spike credentials and substitutes placeholders only for exact destination hosts."
        ),
        config=_openclaw_spike_iron_config(),
        env=[
            SecretRef(namespace=NAME, name="haku-claude-oauth-token")
            .key("CLAUDE_CODE_OAUTH_TOKEN")
            .env_var("CLAUDE_CODE_OAUTH_TOKEN"),
            SecretRef(namespace=NAME, name="haku-forgejo-git").key("password").env_var("HAKU_GIT_PASSWORD"),
            SecretRef(namespace=NAME, name="haku-console-agent-api").key("token").env_var("HAKU_CONSOLE_TOKEN"),
            github_token.env_var("GITHUB_TOKEN"),
            # Rotated roughly every 44 days by agents/authentik-jwt-rotation, which commits the
            # Secret SOPS-encrypted straight into THIS namespace -- deliberately not via the
            # shared claude-sandbox store that carries GITHUB_TOKEN above. That store is
            # conditioned to four namespaces including public-coder-agent, the one deliberately
            # unconfined fence in the cluster; a cluster-API bearer has exactly one consumer and
            # should be readable by exactly one namespace. Reloader's `autoReloadAll` restarts
            # this pod when Flux applies a rotation -- without it, kubectl would start 401ing
            # ~44 days after it last worked with nothing visibly changed.
            # Optional: this proxy is the ONLY egress path for the spike, so a missing kube
            # token must not take out Anthropic, Forgejo and GitHub too. Unset simply means no
            # substitution: kubectl 401s, everything else is untouched.
            kube_token.key("jwt").env_var("HAKU_KUBE_JWT", optional=True),
        ],
    )
    k8s.KubeNetworkPolicy(
        chart,
        "openclaw-spike-networkpolicy",
        metadata=k8s.ObjectMeta(name="allow-haku-openclaw-spike-proxy-ingress", namespace=NAME),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=OPENCLAW_SPIKE_PROXY.pods.selector),
            policy_types=["Ingress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "haku-openclaw-spike"}
                            )
                        )
                    ],
                    ports=[
                        k8s.NetworkPolicyPort(
                            port=k8s.IntOrString.from_number(OPENCLAW_SPIKE_PROXY.pod_port), protocol="TCP"
                        )
                    ],
                )
            ],
        ),
    )
    # The authentik-jwt-rotation CronJob probes the tokens it publishes against their real
    # endpoints each run, which means reading back the published Secret. Its flux-system Role
    # does not cover this namespace, so it needs a read grant here, scoped to the one Secret
    # name. Keep in sync with the k8s_secret name in authentik_jwt_rotation.ROTATIONS.
    k8s.KubeRole(
        chart,
        "kube-token-probe-role",
        metadata=k8s.ObjectMeta(
            name=_PUBLISHED_SECRETS_READER,
            namespace=NAME,
            annotations={
                "description": (
                    "Lets the authentik-jwt-rotation CronJob read back the one Secret it"
                    " publishes here, so its probe can verify the live token against"
                    " kubeapi.allegedly.works. Nothing else in this namespace is readable."
                )
            },
        ),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["secrets"], verbs=["get"], resource_names=[kube_token.name])],
    )
    k8s.KubeRoleBinding(
        chart,
        "kube-token-probe-rolebinding",
        metadata=k8s.ObjectMeta(name=_PUBLISHED_SECRETS_READER, namespace=NAME),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_PUBLISHED_SECRETS_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name="authentik-jwt-rotation", namespace="agents-infra")],
    )


def _sandbox_fence(chart: Chart) -> None:
    """Force all external egress from the haku-sandbox namespace through the dedicated
    haku-egress-proxy. Allows: DNS, cluster-internal traffic, kube-apiserver, and haku-egress-proxy
    port 8080. Blocks: direct external internet access.
    """
    cilium.force_proxy_egress(
        chart,
        "haku-sandbox-force-proxy-egress",
        name="haku-sandbox-force-proxy-egress",
        namespaces=["haku-sandbox"],
        proxy_namespace=SERVICE.pods.namespace,
        proxy_name=NAME,
        proxy_port=SERVICE.pod_port,
        # All cluster-internal traffic (pod-to-service, bypasses proxy via NO_PROXY).
        # This is also how haku-sandbox reaches the Plaid Postgres cluster-internally.
        cluster_ports=None,
        kube_apiserver=True,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAME)
    # Consumer-owned referent identity for source-approved external credentials.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAME)
    )
    _ca(chart)
    _mitmproxy(chart)
    _openclaw_spike_proxy(chart)
    _sandbox_fence(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(
        root,
        OUTPUT_DIR,
        namespaces.haku_egress_proxy,
        chart,
        egress_fences.haku_openclaw_spike,
        egress_fences.haku_cloud_api,
    )
