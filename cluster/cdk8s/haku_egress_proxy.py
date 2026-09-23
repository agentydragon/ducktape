"""haku-egress-proxy (cluster/k8s/agents/haku-egress-proxy): the mitmproxy egress chokepoint
for haku-sandbox and haku-ci, its interception CA and trust bundle, and the two iron-proxy
credential substituters (the Console-owned Claude sandbox's and the OpenClaw spike's).

Written beside other generated files in the same directory (the Namespace from
agents/namespaces.py, the CiliumNetworkPolicies from egress_fences.py). Hand-written there:
`kustomization.yaml` (its configMapGenerator renames the iron configs into `iron.yaml`, and
a patch renames a SOPS Secret), the iron configs themselves, the SOPS Secrets, and
`image-pins/kustomization.yaml`, which overrides the iron-proxy placeholder tag via Flux's
image-automation marker.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecSecretTemplate,
)
from cilium_clusterwide_crds.io.cilium import (
    CiliumClusterwideNetworkPolicy,
    CiliumClusterwideNetworkPolicySpec,
    CiliumClusterwideNetworkPolicySpecEgress,
    CiliumClusterwideNetworkPolicySpecEgressToEndpoints,
    CiliumClusterwideNetworkPolicySpecEgressToEntities,
    CiliumClusterwideNetworkPolicySpecEgressToPorts,
    CiliumClusterwideNetworkPolicySpecEgressToPortsPorts,
    CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumClusterwideNetworkPolicySpecEndpointSelector,
    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions,
    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressionsOperator,
)
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from trust_manager_crds.io.cert_manager.trust import (
    Bundle,
    BundleSpec,
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
    BundleSpecTargetNamespaceSelector,
    BundleSpecTargetNamespaceSelectorMatchExpressions,
)

from cluster.cdk8s import cilium, external_creds
from cluster.cdk8s.external_secrets.external_secret import add_external_secret, remote_data
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "haku-egress-proxy"
OUTPUT_DIR = "cluster/k8s/agents/haku-egress-proxy"
_LABELS = {"app.kubernetes.io/name": NAME}
_CA_SECRET = "haku-egress-proxy-ca"
_IRON_PROXY_IMAGE = "git.allegedly.works/ducktape-ci/iron-proxy:unset"
_CLAUDE_PROXY = "haku-claude-oauth-proxy"
_OPENCLAW_SPIKE_PROXY = "haku-openclaw-spike-proxy"
_PUBLISHED_SECRETS_READER = "authentik-jwt-rotation-published-secrets-reader"


def _quantities(**values: str) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _secret_env(name: str, secret: str, key: str, *, optional: bool | None = None) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name,
        value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret, key=key, optional=optional)),
    )


def _ca(chart: Chart) -> None:
    Certificate(
        chart,
        "certificate",
        metadata=metadata("haku-egress-proxy-root-ca", NAME),
        spec=CertificateSpec(
            is_ca=True,
            common_name="haku-egress-proxy-root-ca",
            secret_name=_CA_SECRET,
            duration="87600h",  # 10 years
            renew_before="8760h",  # 1 year
            private_key=CertificateSpecPrivateKey(algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA, size=256),
            secret_template=CertificateSpecSecretTemplate(
                annotations={
                    "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                    # haku-console: the colocated egress proxy sidecar (#4942) intercepts with this
                    # same shared CA, so fenced sandboxes — which already trust it via
                    # haku-egress-proxy-ca-cert — trust the colocated listener too. When the
                    # iron/mitmproxy fence retires (#4670 end state) this CA's ownership moves out
                    # of this directory with it.
                    "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "cert-manager,haku-console",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "cert-manager,haku-console",
                }
            ),
            issuer_ref=CertificateSpecIssuerRef(name="cluster-ca-bootstrap", kind="ClusterIssuer"),
        ),
    )
    Bundle(
        chart,
        "trust-bundle",
        metadata=ApiObjectMetadata(name="haku-egress-proxy-ca-cert"),
        spec=BundleSpec(
            sources=[
                BundleSpecSources(use_default_c_as=True),
                BundleSpecSources(secret=BundleSpecSourcesSecret(name="cluster-root-ca-secret", key="ca.crt")),
                BundleSpecSources(secret=BundleSpecSourcesSecret(name=_CA_SECRET, key="tls.crt")),
            ],
            target=BundleSpecTarget(
                config_map=BundleSpecTargetConfigMap(
                    key="ca-certificates.crt",
                    metadata=BundleSpecTargetConfigMapMetadata(
                        annotations={
                            "description": "Trust bundle for haku-egress-proxy-inspected sandbox HTTPS traffic"
                        }
                    ),
                ),
                # Written into Haku trust domains. The CLIProxyAPI-backed aiquota path
                # connects directly to the in-cluster management service and does not
                # trust or use this inspected egress listener. public-coder-agent receives
                # it for the #4943 spike: its OpenClaw pod mounts this bundle to verify TLS
                # through the colocated Console egress fence (haku-console:8888).
                namespace_selector=BundleSpecTargetNamespaceSelector(
                    match_expressions=[
                        BundleSpecTargetNamespaceSelectorMatchExpressions(
                            key="kubernetes.io/metadata.name",
                            operator="In",
                            values=["haku-sandbox", "haku-openclaw-spike", "haku-ci", "public-coder-agent"],
                        )
                    ]
                ),
            ),
        ),
    )


_MITMPROXY_CA_INIT_SCRIPT = """\
cat /mitmproxy-ca/tls.key /mitmproxy-ca/tls.crt > /mitmproxy-data/mitmproxy-ca.pem
cp /mitmproxy-ca/tls.crt /mitmproxy-data/mitmproxy-ca-cert.pem
"""


def _mitmproxy(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME, namespace=NAME, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
        ),
        spec=k8s.DeploymentSpec(
            # Two, so one container's restart never empties the Service. mitmproxy OOM-kills
            # under haku-ci traffic (#5846), and with one replica every kill was a CI outage:
            # dependency fetches mid-flight got "connection refused" for the restart's duration.
            replicas=2,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    affinity=k8s.Affinity(
                        pod_anti_affinity=k8s.PodAntiAffinity(
                            # Preferred, not required: this proxy is haku-ci's only egress path, so a
                            # replica left Pending by a hard rule costs more than two on one node.
                            preferred_during_scheduling_ignored_during_execution=[
                                k8s.WeightedPodAffinityTerm(
                                    weight=100,
                                    pod_affinity_term=k8s.PodAffinityTerm(
                                        label_selector=k8s.LabelSelector(match_labels=_LABELS),
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
                                "8080",
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
                            ports=[k8s.ContainerPort(name="proxy", container_port=8080)],
                            # An endpoint only once the listener is up: with two replicas a rolling
                            # update otherwise routes to a pod that is not listening yet.
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string("proxy")),
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
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[k8s.ServicePort(name="proxy", port=8080, target_port=k8s.IntOrString.from_number(8080))],
        ),
    )
    # With two replicas, a voluntary disruption (node drain, descheduler eviction, rolling
    # update) may take one proxy pod at a time but never both, so haku-ci's only egress path
    # keeps a ready endpoint throughout. The descheduler honors PDBs unconditionally.
    k8s.KubePodDisruptionBudget(
        chart,
        "poddisruptionbudget",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(1), selector=k8s.LabelSelector(match_labels=_LABELS)
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
            pod_selector=k8s.LabelSelector(match_labels=_LABELS),
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
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(8080), protocol="TCP")],
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


def _iron_proxy(
    chart: Chart, name: str, *, description: str, config_map: str, port: int, env: list[k8s.EnvVar]
) -> None:
    """An iron-proxy Deployment holding real credentials and substituting them for a sandbox's
    placeholders, plus its Service."""
    labels = {"app.kubernetes.io/name": name}
    k8s.KubeDeployment(
        chart,
        f"{name}-deployment",
        metadata=k8s.ObjectMeta(
            name=name,
            namespace=NAME,
            labels=labels,
            annotations={"description": description, "reloader.stakater.com/auto": "true"},
        ),
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
                            ports=[
                                k8s.ContainerPort(name="proxy", container_port=port),
                                k8s.ContainerPort(name="metrics", container_port=9090),
                            ],
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
                        # Rendered from the iron config by the hand-written kustomization's
                        # configMapGenerator.
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=config_map)),
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
            selector=labels,
            ports=[
                k8s.ServicePort(name="proxy", port=port, target_port=k8s.IntOrString.from_string("proxy")),
                k8s.ServicePort(name="metrics", port=9090, target_port=k8s.IntOrString.from_string("metrics")),
            ],
        ),
    )


def _github_token(chart: Chart, name: str) -> None:
    """The agentydragon-agent GitHub PAT, consumed only by one iron-proxy here; its sandbox
    receives a non-secret placeholder that the proxy replaces in Authorization headers for
    exact GitHub hosts."""
    add_external_secret(
        chart,
        name,
        name=name,
        namespace=NAME,
        refresh="1h",
        store=external_creds.STORE,
        data=[remote_data("github-agentydragon-agent", "token", secret_key="GITHUB_TOKEN")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def _claude_proxy(chart: Chart) -> None:
    # For the Console-owned Claude sandbox. Same remote PAT the OpenClaw spike reads — one
    # account, separately delivered, so retiring the spike does not take this with it.
    github_token = "haku-claude-github-token"
    _github_token(chart, github_token)
    _iron_proxy(
        chart,
        _CLAUDE_PROXY,
        description=(
            "Holds the real Claude subscription OAuth token and substitutes it for the sandbox"
            " placeholder only on api.anthropic.com Authorization headers."
        ),
        config_map="haku-claude-oauth-proxy-config",
        port=8180,
        env=[
            _secret_env("CLAUDE_CODE_OAUTH_TOKEN", "haku-claude-oauth-token", "CLAUDE_CODE_OAUTH_TOKEN"),
            _secret_env("GITHUB_TOKEN", github_token, "GITHUB_TOKEN"),
            # The same bearer aiquota-api authenticates with, reflected here from
            # cli-proxy-api solely for iron-proxy to substitute into the sandbox's
            # placeholder on the two read endpoints; the runtime never receives it.
            # Optional: this proxy is the ONLY egress path for haku-runtime-sandbox, so a
            # late-reflecting secret must not take Anthropic and GitHub down with it. Unset
            # simply means no substitution — aiquota 401s, everything else is untouched.
            _secret_env("AIQUOTA_API_BEARER_TOKEN", "aiquota-api-bearer-haku-claude", "bearer-token", optional=True),
            # The central ActivityWatch read-only bearer, reflected here from the
            # activitywatch namespace solely for iron-proxy to substitute into the sandbox's
            # placeholder on the read route. Optional for the same reason as above.
            _secret_env("AW_READ_TOKEN", "activitywatch-read-token", "token", optional=True),
        ],
    )
    k8s.KubeNetworkPolicy(
        chart,
        "claude-networkpolicy",
        metadata=k8s.ObjectMeta(name="allow-haku-claude-oauth-proxy-ingress", namespace=NAME),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": _CLAUDE_PROXY}),
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "haku-runtime-sandbox"}
                            ),
                            pod_selector=k8s.LabelSelector(
                                match_labels={
                                    "app.kubernetes.io/name": "haku-harness-runner",
                                    "haku.allegedly.works/access-profile-id": "haku",
                                }
                            ),
                        )
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(8180), protocol="TCP")],
                )
            ],
            policy_types=["Ingress"],
        ),
    )


def _openclaw_spike_proxy(chart: Chart) -> None:
    # Shared with public-coder-agent's copy of the same PAT.
    github_token = "haku-openclaw-spike-github-token"
    kube_token = "haku-openclaw-spike-kube-token"
    _github_token(chart, github_token)
    _iron_proxy(
        chart,
        _OPENCLAW_SPIKE_PROXY,
        description=(
            "Holds Haku OpenClaw spike credentials and substitutes placeholders only for exact destination hosts."
        ),
        config_map="haku-openclaw-spike-proxy-config",
        port=8181,
        env=[
            _secret_env("CLAUDE_CODE_OAUTH_TOKEN", "haku-claude-oauth-token", "CLAUDE_CODE_OAUTH_TOKEN"),
            _secret_env("HAKU_GIT_PASSWORD", "haku-forgejo-git", "password"),
            _secret_env("HAKU_CONSOLE_TOKEN", "haku-console-agent-api", "token"),
            _secret_env("GITHUB_TOKEN", github_token, "GITHUB_TOKEN"),
            # Rotated roughly every 44 days by agents/authentik-jwt-rotation, which commits the
            # Secret SOPS-encrypted straight into THIS namespace -- deliberately not via the
            # shared claude-sandbox store that carries GITHUB_TOKEN above. That store is
            # conditioned to four namespaces including public-coder-agent, the one deliberately
            # unconfined fence in the cluster; a cluster-API bearer has exactly one consumer and
            # should be readable by exactly one namespace. reloader.stakater.com/auto restarts
            # this pod when Flux applies a rotation -- without it, kubectl would start 401ing
            # ~44 days after it last worked with nothing visibly changed.
            # Optional: this proxy is the ONLY egress path for the spike, so a missing kube
            # token must not take out Anthropic, Forgejo and GitHub too. Unset simply means no
            # substitution: kubectl 401s, everything else is untouched.
            _secret_env("HAKU_KUBE_JWT", kube_token, "jwt", optional=True),
        ],
    )
    k8s.KubeNetworkPolicy(
        chart,
        "openclaw-spike-networkpolicy",
        metadata=k8s.ObjectMeta(name="allow-haku-openclaw-spike-proxy-ingress", namespace=NAME),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": _OPENCLAW_SPIKE_PROXY}),
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
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(8181), protocol="TCP")],
                )
            ],
        ),
    )
    # The authentik-jwt-rotation CronJob probes the tokens it publishes against their real
    # endpoints each run, which means reading back the published Secret. Its flux-system Role
    # does not cover this namespace, so it needs a read grant here, scoped to the one Secret
    # name. Keep in sync with the k8s_secret name in rotations.yaml.
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
        rules=[k8s.PolicyRule(api_groups=[""], resources=["secrets"], verbs=["get"], resource_names=[kube_token])],
    )
    k8s.KubeRoleBinding(
        chart,
        "kube-token-probe-rolebinding",
        metadata=k8s.ObjectMeta(name=_PUBLISHED_SECRETS_READER, namespace=NAME),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_PUBLISHED_SECRETS_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name="authentik-jwt-rotation", namespace="agents-infra")],
    )


_TCP = CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol.TCP
_UDP = CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol.UDP


def _ports(
    *ports: tuple[int, CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol],
) -> list[CiliumClusterwideNetworkPolicySpecEgressToPorts]:
    return [
        CiliumClusterwideNetworkPolicySpecEgressToPorts(
            ports=[
                CiliumClusterwideNetworkPolicySpecEgressToPortsPorts(port=str(number), protocol=protocol)
                for number, protocol in ports
            ]
        )
    ]


def _to_endpoint(namespace: str, labels: dict[str, str], port: int) -> CiliumClusterwideNetworkPolicySpecEgress:
    """Egress to the pods carrying `labels` in `namespace`, on TCP `port`."""
    return CiliumClusterwideNetworkPolicySpecEgress(
        to_endpoints=[
            CiliumClusterwideNetworkPolicySpecEgressToEndpoints(
                match_labels={"k8s:io.kubernetes.pod.namespace": namespace, **labels}
            )
        ],
        to_ports=_ports((port, _TCP)),
    )


# DNS resolution (CoreDNS in kube-system)
_DNS = CiliumClusterwideNetworkPolicySpecEgress(
    to_endpoints=[CiliumClusterwideNetworkPolicySpecEgressToEndpoints(match_labels=cilium.KUBE_DNS_LABELS)],
    to_ports=_ports((53, _UDP), (53, _TCP)),
)


def _namespace_selector(namespace: str) -> CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions:
    return CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions(
        key="k8s:io.kubernetes.pod.namespace",
        operator=CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressionsOperator.IN,
        values=[namespace],
    )


def _sandbox_fence(chart: Chart) -> None:
    """Force all external egress from the haku-sandbox namespace through the dedicated
    haku-egress-proxy. Allows: DNS, cluster-internal traffic, kube-apiserver, haku-egress-proxy
    port 8080, and the colocated egress proxy in the Console pod (haku-console, port 8888,
    #4942). Blocks: direct external internet access.

    The colocated-proxy rule is explicit even though the `toEntities: cluster` rule already admits
    it at L4: it keeps the enforcement model legible (#4670 § Enforcement topology -- "DNS,
    cluster, apiserver, and the proxy's listener") and survives the eventual tightening of that
    broad cluster rule into a ceiling. It makes the colocated listener *reachable*; the
    Kyverno-injected HTTP_PROXY still points sandbox clients at the port-8080 fence, so this opens
    the path without cutting traffic over (the repoint is the adoption step). The oracle at
    haku-console:8079 is loopback-bound, so nothing answers on the pod IP there.
    """
    CiliumClusterwideNetworkPolicy(
        chart,
        "haku-sandbox-force-proxy-egress",
        metadata=ApiObjectMetadata(name="haku-sandbox-force-proxy-egress"),
        spec=CiliumClusterwideNetworkPolicySpec(
            endpoint_selector=CiliumClusterwideNetworkPolicySpecEndpointSelector(
                match_expressions=[_namespace_selector("haku-sandbox")]
            ),
            egress=[
                _DNS,
                # All cluster-internal traffic (pod-to-service, bypasses proxy via NO_PROXY).
                # This is also how haku-sandbox reaches the Plaid Postgres cluster-internally.
                CiliumClusterwideNetworkPolicySpecEgress(
                    to_entities=[CiliumClusterwideNetworkPolicySpecEgressToEntities.CLUSTER]
                ),
                # Kubernetes API server
                CiliumClusterwideNetworkPolicySpecEgress(
                    to_entities=[CiliumClusterwideNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER],
                    to_ports=_ports((6443, _TCP)),
                ),
                # Shared proxy for existing sandbox traffic.
                _to_endpoint(NAME, {"k8s:app.kubernetes.io/name": NAME}, 8080),
                # Colocated egress proxy in the Console pod (#4942). The sidecar shares the Console
                # pod's network namespace, so its listener is selected by the Console pod label on
                # port 8888. Once the Kyverno HTTP_PROXY repoint lands, this becomes the sandbox's
                # egress path; until then it is a reachable-but-unused route the adoption cutover
                # switches to.
                _to_endpoint("haku-console", {"k8s:app.kubernetes.io/name": "haku-console"}, 8888),
            ],
        ),
    )


def _agent_runner_fence(chart: Chart) -> None:
    """Console-owned Haku Agent runners are isolated from Haku's general sandbox namespace. They
    can reach Haku Console's runner-protocol endpoint, the OAuth-substituting proxy, the in-cluster
    Forgejo they check haku-state out of, and the Console's authorization proxy -- but have no
    direct internet, general cluster access, or general-purpose proxy."""
    CiliumClusterwideNetworkPolicy(
        chart,
        "haku-agent-runner-egress",
        metadata=ApiObjectMetadata(name="haku-agent-runner-egress"),
        spec=CiliumClusterwideNetworkPolicySpec(
            endpoint_selector=CiliumClusterwideNetworkPolicySpecEndpointSelector(
                match_labels={
                    "app.kubernetes.io/name": "haku-harness-runner",
                    "haku.allegedly.works/access-profile-id": "haku",
                },
                match_expressions=[_namespace_selector("haku-runtime-sandbox")],
            ),
            egress=[
                _DNS,
                # Cilium evaluates the destination endpoint after Service translation.
                # haku-console Service port 9090 targets the API container's `api` port 8080.
                _to_endpoint("haku-console", {"k8s:app.kubernetes.io/name": "haku-console"}, 8080),
                _to_endpoint(NAME, {"k8s:app.kubernetes.io/name": _CLAUDE_PROXY}, 8180),
                # Colocated Console egress fence (#4670): the runner's HTTPS_PROXY now points here
                # (haku-egress-proxy.haku-console.svc:8888), carrying its inference to the
                # in-cluster LiteLLM gateway and its GitHub traffic. The sidecar shares the Console
                # pod's network namespace, so it is selected by the Console pod label on the
                # proxy's container port. The haku-claude-oauth-proxy rule above stays until that
                # iron proxy retires, so reverting the runner's proxy needs no policy change.
                _to_endpoint("haku-console", {"k8s:app.kubernetes.io/name": "haku-console"}, 8888),
                # Kubernetes access is mediated by the Haku Console authorization proxy. The runner
                # writes an ephemeral tokenFile kubeconfig only when Console selects this route; no
                # ServiceAccount token is mounted in the runner pod and there is no direct
                # apiserver path. The proxy's TLS listener, because kubectl sends credentials only
                # to an https server.
                _to_endpoint("haku-console", {"k8s:app.kubernetes.io/name": "haku-kube-api-proxy"}, 8443),
                # haku-state, so the session starts with Haku's manual rather than an empty
                # workspace. In-cluster and plaintext, the way the haku-sandbox exec target already
                # clones it -- so no credential passes through the OAuth-substituting proxy, which
                # knows one host and one header and has no business rewriting git auth. The runner
                # writes the same haku Forgejo account into ~/.netrc; a public git.allegedly.works
                # route is deliberately NOT opened, since it would hairpin out through the Gateway
                # for a service one hop away.
                _to_endpoint("forgejo", {"k8s:app.kubernetes.io/name": "forgejo"}, 3000),
            ],
        ),
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
    _claude_proxy(chart)
    _openclaw_spike_proxy(chart)
    _sandbox_fence(chart)
    _agent_runner_fence(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
