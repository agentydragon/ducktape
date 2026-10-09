"""haku-mailbox: the Stalwart mailserver holding Haku's receive-only mailbox
(haku@allegedly.works), its Postgres store, STARTTLS certificate, public HTTP route and the
per-public-node SMTP ingress.

Hand-written beside the output: the SOPS Secrets, the `configMapGenerator` inputs and
`image-pins/kustomization.yaml`, which overrides the Stalwart image's `unset` tag.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cert_manager_crds.io.cert_manager import CertificateSpecIssuerRef
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)

from cluster.cdk8s import cilium, cnpg, gateway, namespaces, node_scheduling
from cluster.cdk8s.cert_manager.config import LETSENCRYPT_ISSUER
from cluster.cdk8s.flux import ConfigMapArgs, GeneratorOptions
from cluster.cdk8s.forgejo_registry import chart as forgejo_images
from cluster.cdk8s.haku import namespace
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cert_manager.certificate import Certificate
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import (
    ClusterExternalSecret,
    ClusterSecretStoreRef,
    cluster_remote_data,
)
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "haku-mailbox"
NAMESPACE = "haku-mailbox"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/haku/mailbox"

_INGRESS_NAME = "haku-mailbox-smtp-ingress"  # the ingress pods' label value and nginx ConfigMap name
_INGRESS_LABELS = {"app.kubernetes.io/name": _INGRESS_NAME}
_INGRESS_OBJECT_NAME = "smtp-ingress"  # the DaemonSet and its CiliumNetworkPolicy
_TLS_SECRET = "mx-allegedly-works-tls"
DATABASE = cnpg.PostgresRef.generated(name="haku-mailbox-db", namespace=NAMESPACE)
_DB_PASSWORD = DATABASE.app_secret.key("password")
_PUBLIC_URL = "https://haku-mailbox.allegedly.works"
# In-repo repack of stalwartlabs/stalwart with stalwart-cli layered in
# (//cluster/k8s/haku/mailbox/image) -- upstream ships the CLI only as a distroless image,
# unusable from the pod. Published by the push-images workflow; image-pins/ sets the tag Flux
# image automation tracks.
_IMAGE = "git.allegedly.works/ducktape-ci/stalwart:unset"
# Stalwart's SMTP listener, and the one the ingress's nginx.conf listens on too.
_SMTP_PORT = 2525
# Stalwart's HTTP listener: JMAP and the management API.
_HTTP = ServiceRef(
    name=NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
_IMAP = ServiceRef(name=_HTTP.name, port=Port(name="imap", number=1143), pods=_HTTP.pods)
_SMTP = ServiceRef(name="smtp", port=Port(name="smtp", number=_SMTP_PORT), pods=_HTTP.pods)
_CONFIG_DIR = "/etc/stalwart"  # where CONFIG_MAP is mounted
_INITIALIZE = "initialize.sh"
_SERVER_CONFIG = "config.json"
# The provisioning plan: the server's config, the init container's script and the plan it applies.
CONFIG_MAP = ConfigMapArgs(
    name="haku-mailbox-config",
    namespace=NAMESPACE,
    # The script and the plan's Sieve carry `${...}` that are theirs, not Flux's.
    options=GeneratorOptions(annotations={"kustomize.toolkit.fluxcd.io/substitute": "disabled"}),
    files=[_INITIALIZE, _SERVER_CONFIG, "mailbox-plan.ndjson"],
)
INGRESS_CONFIG_MAP = ConfigMapArgs(name=_INGRESS_NAME, namespace=NAMESPACE, files=["nginx.conf"])
# No namespace transformer: haku-mail-token.sops.yaml targets flux-system (the rotator's
# publication point); everything else carries its namespace explicitly.
SOPS_FILES = ("haku-mailbox-admin.sops.yaml", "haku-mail-token.sops.yaml")


def _quantities(values: dict[str, str]) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _stalwart_resources() -> k8s.ResourceRequirements:
    return k8s.ResourceRequirements(
        requests=_quantities({"cpu": "100m", "memory": "256Mi"}), limits=_quantities({"cpu": "1", "memory": "1Gi"})
    )


def _stalwart_mounts() -> list[k8s.VolumeMount]:
    return [
        k8s.VolumeMount(name="config", mount_path=_CONFIG_DIR, read_only=True),
        k8s.VolumeMount(name="tls", mount_path="/tls", read_only=True),
        k8s.VolumeMount(name="tmp", mount_path="/tmp"),
    ]


def _curl_probe(path: str, *, period_seconds: int, failure_threshold: int | None = None) -> k8s.Probe:
    return k8s.Probe(
        exec=k8s.ExecAction(command=["curl", "--fail", "--silent", f"http://127.0.0.1:{_HTTP.pod_port}{path}"]),
        period_seconds=period_seconds,
        failure_threshold=failure_threshold,
    )


def _add_store(chart: Chart) -> None:
    # Store for the Stalwart mailserver (data + blobs + search + settings all live in Postgres --
    # no PVC on the app; see cluster/k8s/haku/mailbox/README.md). OVH-HA CNPG profile per
    # cluster/docs/cnpg_conventions.md: mail must stay OVH-resilient.
    cnpg.cluster(
        chart,
        "db",
        ref=DATABASE,
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh",
        size="10Gi",
        initdb=cnpg.same_owner_initdb("stalwart"),
        wal_archive=False,
    )


def _add_deployment(chart: Chart) -> None:
    """Haku is a mail *user*, never the server admin: all policy (the SPF-gated sender whitelist,
    listeners, the OIDC directory) is the operator-owned provisioning plan mounted into this Pod.
    An init container applies it with a temporary fallback admin before production starts.
    Authentication is exclusively Authentik OIDC bearer tokens (the stalwart-haku provider); no
    mailbox password exists."""
    public_url = k8s.EnvVar(name="STALWART_PUBLIC_URL", value=_PUBLIC_URL)
    # Reloader's `autoReloadAll` restarts this on rotation of the mounted STARTTLS certificate and
    # DB credentials so the normal server re-reads them.
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=_HTTP.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_HTTP.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_HTTP.pods.selector),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
                    automount_service_account_token=False,
                    # OVH-only resilience: inbound mail must not depend on Proxmox, and the CNPG
                    # store is pinned to hil-ovh -- co-locate with it (same pin as other
                    # OVH-pinned apps, e.g. paperless).
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True, run_as_user=1000, run_as_group=1000, fs_group=1000
                    ),
                    init_containers=[
                        k8s.Container(
                            name="initialize",
                            image=_IMAGE,
                            command=["/bin/sh", f"{_CONFIG_DIR}/{_INITIALIZE}"],
                            termination_message_policy="FallbackToLogsOnError",
                            env=[
                                _DB_PASSWORD.env_var("STALWART_DB_PASSWORD"),
                                SecretRef(namespace=NAMESPACE, name="haku-mailbox-admin")
                                .key("password")
                                .env_var("STALWART_ADMIN_PASSWORD"),
                                public_url,
                            ],
                            volume_mounts=_stalwart_mounts(),
                            resources=_stalwart_resources(),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(add=["NET_BIND_SERVICE"], drop=["ALL"]),
                            ),
                        )
                    ],
                    containers=[
                        k8s.Container(
                            name="stalwart",
                            image=_IMAGE,
                            command=["/usr/local/bin/stalwart", "--config", f"{_CONFIG_DIR}/{_SERVER_CONFIG}"],
                            # Surface crash output in pod status (.lastState.terminated.message):
                            # pods/log in this namespace is RBAC-fenced to the operator, but pod
                            # status is diagnostics-readable -- without this, an initialization
                            # failure is a black box to agent sessions.
                            termination_message_policy="FallbackToLogsOnError",
                            ports=[
                                _SMTP.port.k8s_container_port(),
                                _HTTP.port.k8s_container_port(),
                                _IMAP.port.k8s_container_port(),
                            ],
                            env=[_DB_PASSWORD.env_var("STALWART_DB_PASSWORD"), public_url],
                            volume_mounts=_stalwart_mounts(),
                            resources=_stalwart_resources(),
                            # Loopback exec probes avoid the kubelet-to-pod-IP path that caused
                            # healthy Stalwart endpoints to reset and be SIGTERMed repeatedly.
                            startup_probe=_curl_probe("/healthz/live", period_seconds=5, failure_threshold=30),
                            liveness_probe=_curl_probe("/healthz/live", period_seconds=30),
                            readiness_probe=_curl_probe("/healthz/ready", period_seconds=10),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="config",
                            config_map=k8s.ConfigMapVolumeSource(name=CONFIG_MAP.name, default_mode=0o555),
                        ),
                        k8s.Volume(name="tls", secret=k8s.SecretVolumeSource(secret_name=_TLS_SECRET)),
                        k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                    ],
                ),
            ),
        ),
    )


def _add_services(chart: Chart) -> None:
    # Two internal Services on purpose: SMTP is reachable only from the per-node TCP ingress
    # DaemonSet, while HTTP and IMAP keep their existing consumers. Public port 25 is provided by
    # the ingress DaemonSet's hostPort, not externalIPs: cross-node externalIP forwarding would
    # SNAT the sending MTA and break SPF.
    k8s.KubeService(
        chart,
        "smtp-service",
        metadata=k8s.ObjectMeta(
            name=_SMTP.name,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Cluster-internal SMTP backend for the per-node smtp-ingress proxies. "
                    "The proxies carry the sending MTA address in PROXY protocol so Stalwart can "
                    "evaluate SPF against the real peer."
                )
            },
        ),
        spec=k8s.ServiceSpec(selector=_SMTP.pods.selector, ports=[_SMTP.port.k8s_service_port()]),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(
            name=_HTTP.name,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Stalwart's cluster-internal listeners: HTTP (JMAP + management API, exposed "
                    "publicly only via the cluster-gateway HTTPRoute haku-mailbox.allegedly.works) and "
                    "plaintext IMAP for haku-sandbox clients (himalaya) — no route, no externalIPs, "
                    "OAUTHBEARER-only auth."
                )
            },
        ),
        spec=k8s.ServiceSpec(
            selector=_HTTP.pods.selector, ports=[_HTTP.port.k8s_service_port(), _IMAP.port.k8s_service_port()]
        ),
    )


def _add_smtp_ingress(chart: Chart) -> None:
    """Raw-TCP SMTP ingress on every public OVH node. This is the port-25 analogue of the
    per-node hostNetwork Envoy tier serving HTTP: each node accepts the public connection
    locally, then proxies to the movable Stalwart backend. PROXY protocol is mandatory because
    SPF depends on the sending MTA's address."""
    k8s.KubeDaemonSet(
        chart,
        "smtp-ingress",
        metadata=k8s.ObjectMeta(
            name=_INGRESS_OBJECT_NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Per-public-node port-25 TCP ingress. Preserves the sending MTA address through "
                    "PROXY protocol so Stalwart's SPF gate remains meaningful."
                )
            },
        ),
        spec=k8s.DaemonSetSpec(
            selector=k8s.LabelSelector(match_labels=_INGRESS_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_INGRESS_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    # Same public-node selector as Cilium's hostNetwork Gateway Envoy tier.
                    node_selector={"topology.kubernetes.io/region": "hil"},
                    # This is the per-public-node MX entry point: hostPort 25 must remain present
                    # on every public OVH node while the control-plane taint is rolled out. The
                    # backend Deployment is movable; this DaemonSet is the explicit control-plane
                    # exception until the public-node roster is redesigned.
                    tolerations=[node_scheduling.CONTROL_PLANE_TOLERATION],
                    containers=[
                        k8s.Container(
                            name="nginx",
                            image="nginxinc/nginx-unprivileged:1.31-alpine@sha256:e1754f434ace974cdf9b9f98d868082b86ab8ee703f8466e6d3d777f553d3fb9",
                            ports=[
                                k8s.ContainerPort(name="smtp", container_port=_SMTP_PORT, host_port=25, protocol="TCP")
                            ],
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string("smtp")),
                                period_seconds=10,
                            ),
                            liveness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string("smtp")),
                                period_seconds=30,
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="config",
                                    mount_path="/etc/nginx/nginx.conf",
                                    sub_path="nginx.conf",
                                    read_only=True,
                                ),
                                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                                k8s.VolumeMount(name="cache", mount_path="/var/cache/nginx"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests=_quantities({"cpu": "10m", "memory": "16Mi"}),
                                limits=_quantities({"memory": "64Mi"}),
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=INGRESS_CONFIG_MAP.name)),
                        k8s.Volume(
                            name="tmp",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("1Mi")
                            ),
                        ),
                        k8s.Volume(
                            name="cache",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("8Mi")
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )
    # The broad CIDR trusted by Stalwart for PROXY headers is safe only together with these
    # identity-aware policies: only the ingress DaemonSet may reach the SMTP backend, so another
    # pod cannot forge a Google source address.
    NetworkPolicy(
        chart,
        "smtp-ingress-policy",
        metadata=ApiObjectMetadata(name=_INGRESS_OBJECT_NAME, namespace=NAMESPACE),
        endpoint_selector=_INGRESS_LABELS,
        ingress=[
            CiliumNetworkPolicySpecIngress(
                from_entities=[
                    CiliumNetworkPolicySpecIngressFromEntities.WORLD,
                    # Kubelet TCP readiness/liveness probes originate from the node.
                    CiliumNetworkPolicySpecIngressFromEntities.HOST,
                ],
                to_ports=[
                    CiliumNetworkPolicySpecIngressToPorts(
                        ports=[
                            CiliumNetworkPolicySpecIngressToPortsPorts(
                                port=str(_SMTP_PORT), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                            )
                        ]
                    )
                ],
            )
        ],
        egress=[cilium.dns_egress(), EgressRule.to_endpoints(_SMTP.pods.selector, _SMTP.pod_port)],
    )
    NetworkPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        endpoint_selector=_HTTP.pods.selector,
        ingress=[
            IngressRule.from_endpoints(_INGRESS_LABELS, ports=[_SMTP.pod_port]),
            IngressRule.from_gateway(_HTTP.pod_port),
            IngressRule.from_endpoints(
                {"k8s:io.kubernetes.pod.namespace": namespace.NAMESPACE}, ports=[_IMAP.pod_port]
            ),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # The Haku mailbox's own trusted namespace -- deliberately NOT haku-sandbox. The Stalwart
    # mailserver here enforces the inbound-email perimeter (DMARC-gated sender whitelist,
    # operator-owned provisioning plan), so it sits OUTSIDE Haku's RBAC and OUTSIDE the
    # haku-egress-proxy egress fence: Haku must not be able to patch the server, edit the
    # whitelist, read the admin/TLS secrets, or touch the CNPG store. Haku is a mail *user* only,
    # authenticated via its Authentik-issued bearer. See cluster/k8s/haku/mailbox/README.md.
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.AUTO,
        labels={
            "name": NAMESPACE,
            # The per-public-node SMTP ingress must bind hostPort 25. Pod Security's baseline
            # profile forbids every hostPort, so this trusted, operator-only namespace needs
            # privileged admission even though its workloads retain restrictive container
            # security contexts and Cilium policies.
            "pod-security.kubernetes.io/enforce": "privileged",
        },
    )
    _add_store(chart)
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    Certificate(
        chart,
        "certificate",
        metadata=ApiObjectMetadata(
            name="mx-allegedly-works",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "STARTTLS certificate for the inbound SMTP listener (mx.allegedly.works). Sending MTAs "
                    "(Gmail) use opportunistic TLS; Reloader restarts the receiver when cert-manager rotates "
                    "this."
                )
            },
        ),
        secret_name=_TLS_SECRET,
        dns_names=["mx.allegedly.works"],
        issuer_ref=CertificateSpecIssuerRef(name=LETSENCRYPT_ISSUER, kind="ClusterIssuer"),
    )
    _add_deployment(chart)
    _add_services(chart)
    _add_smtp_ingress(chart)
    gateway.https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(
            name=NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Public route to Stalwart's HTTP listener: JMAP for haku (authenticated with its "
                    "Authentik-issued bearer, validated by the server's OIDC directory) plus the "
                    "management API/WebUI (admin credential only, which haku cannot read). SMTP (:25) "
                    "enters through the per-node TCP ingress DaemonSet, not the HTTP gateway."
                )
            },
        ),
        hostnames=["haku-mailbox.allegedly.works"],
        backend=_HTTP,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    # Mirror the rotator-published mailbox JWT into haku-sandbox, where Haku reads it to
    # authenticate to its mailbox over JMAP (haku-state/sources/mailbox.md). Same ESO pattern as the
    # grocy-sf token (haku/managed-agent/grocy-token-eso.yaml): it sources the flux-system Secret
    # the rotator writes (k8s_secret output) and refreshes continuously, so the rotated token
    # propagates without any rotator-side distribution config.
    ClusterExternalSecret(
        chart,
        "mail-token",
        metadata=ApiObjectMetadata(name="haku-mail-token"),
        namespaces=[namespace.NAMESPACE],
        secret_store_ref=ClusterSecretStoreRef.cluster("kubernetes-flux-system-secret-store"),
        refresh_interval="1m",
        data=[cluster_remote_data("haku-mail-token", "jwt")],
    )
    return chart
