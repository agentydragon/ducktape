"""haku-mailbox: the Stalwart mailserver holding Haku's receive-only mailbox
(haku@allegedly.works), its Postgres store, STARTTLS certificate, public HTTP route and the
per-public-node SMTP ingress.

Hand-written beside the output: the SOPS Secrets, the `configMapGenerator` inputs and the
directory's `kustomization.yaml` (its generator options are not expressible here), and
`image-pins/kustomization.yaml`, which overrides the Stalwart image's `unset` tag.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cert_manager_crds.io.cert_manager import Certificate, CertificateSpec, CertificateSpecIssuerRef
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from cnpg_cluster_crds.io.cnpg.postgresql import ClusterSpecBootstrapInitdb
from external_secrets_clusterexternalsecret_crds.io.external_secrets import (
    ClusterExternalSecret,
    ClusterExternalSecretSpec,
    ClusterExternalSecretSpecExternalSecretSpec,
    ClusterExternalSecretSpecExternalSecretSpecData,
    ClusterExternalSecretSpecExternalSecretSpecDataRemoteRef,
    ClusterExternalSecretSpecExternalSecretSpecSecretStoreRef,
    ClusterExternalSecretSpecExternalSecretSpecSecretStoreRefKind,
    ClusterExternalSecretSpecExternalSecretSpecTarget,
)

from cluster.cdk8s import cilium, cnpg, forgejo_images, gateway
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.haku import namespace
from cluster.cdk8s.metadata import metadata

NAME = "haku-mailbox"
NAMESPACE = "haku-mailbox"
OUTPUT_DIR = "cluster/k8s/haku/mailbox"

_LABELS = {"app.kubernetes.io/name": NAME}
_INGRESS_NAME = "haku-mailbox-smtp-ingress"
_INGRESS_LABELS = {"app.kubernetes.io/name": _INGRESS_NAME}
_TLS_SECRET = "mx-allegedly-works-tls"
_DB_SECRET = "haku-mailbox-db-app"  # CNPG-generated app credentials
_PUBLIC_URL = "https://haku-mailbox.allegedly.works"
# In-repo repack of stalwartlabs/stalwart with stalwart-cli layered in
# (//cluster/k8s/haku/mailbox/image) -- upstream ships the CLI only as a distroless image,
# unusable from the pod. Published by the push-images workflow; image-pins/ sets the tag Flux
# image automation tracks.
_IMAGE = "git.allegedly.works/ducktape-ci/stalwart:unset"
_SMTP_PORT = 2525
_HTTP_PORT = 8080
_IMAP_PORT = 1143


def _quantities(values: dict[str, str]) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _stalwart_resources() -> k8s.ResourceRequirements:
    return k8s.ResourceRequirements(
        requests=_quantities({"cpu": "100m", "memory": "256Mi"}), limits=_quantities({"cpu": "1", "memory": "1Gi"})
    )


def _stalwart_mounts() -> list[k8s.VolumeMount]:
    return [
        k8s.VolumeMount(name="config", mount_path="/etc/stalwart", read_only=True),
        k8s.VolumeMount(name="tls", mount_path="/tls", read_only=True),
        k8s.VolumeMount(name="tmp", mount_path="/tmp"),
    ]


def _db_password_env() -> k8s.EnvVar:
    return k8s.EnvVar(
        name="STALWART_DB_PASSWORD",
        value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=_DB_SECRET, key="password")),
    )


def _curl_probe(path: str, *, period_seconds: int, failure_threshold: int | None = None) -> k8s.Probe:
    return k8s.Probe(
        exec=k8s.ExecAction(command=["curl", "--fail", "--silent", f"http://127.0.0.1:{_HTTP_PORT}{path}"]),
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
        name="haku-mailbox-db",
        namespace=NAMESPACE,
        affinity=cnpg.affinity(node_selector={"topology.kubernetes.io/zone": "hil-ovh"}, tolerate_control_plane=False),
        storage_class="local-path-ovh",
        size="10Gi",
        # CNPG auto-generates credentials in secret haku-mailbox-db-app.
        initdb=ClusterSpecBootstrapInitdb(database="stalwart", owner="stalwart"),
    )


def _add_deployment(chart: Chart) -> None:
    """Haku is a mail *user*, never the server admin: all policy (the SPF-gated sender whitelist,
    listeners, the OIDC directory) is the operator-owned provisioning plan mounted into this Pod.
    An init container applies it with a temporary fallback admin before production starts.
    Authentication is exclusively Authentik OIDC bearer tokens (the stalwart-haku provider); no
    mailbox password exists."""
    public_url = k8s.EnvVar(name="STALWART_PUBLIC_URL", value=_PUBLIC_URL)
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            labels=_LABELS,
            # Restart on rotation of the mounted STARTTLS certificate and DB credentials so the
            # normal server re-reads them.
            annotations={"reloader.stakater.com/auto": "true"},
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
                    automount_service_account_token=False,
                    # OVH-only resilience: inbound mail must not depend on Proxmox, and the CNPG
                    # store is pinned to hil-ovh -- co-locate with it (same pin as other
                    # OVH-pinned apps, e.g. paperless).
                    node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True, run_as_user=1000, run_as_group=1000, fs_group=1000
                    ),
                    init_containers=[
                        k8s.Container(
                            name="initialize",
                            image=_IMAGE,
                            command=["/bin/sh", "/etc/stalwart/initialize.sh"],
                            termination_message_policy="FallbackToLogsOnError",
                            env=[
                                _db_password_env(),
                                k8s.EnvVar(
                                    name="STALWART_ADMIN_PASSWORD",
                                    value_from=k8s.EnvVarSource(
                                        secret_key_ref=k8s.SecretKeySelector(name="haku-mailbox-admin", key="password")
                                    ),
                                ),
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
                            command=["/usr/local/bin/stalwart", "--config", "/etc/stalwart/config.json"],
                            # Surface crash output in pod status (.lastState.terminated.message):
                            # pods/log in this namespace is RBAC-fenced to the operator, but pod
                            # status is diagnostics-readable -- without this, an initialization
                            # failure is a black box to agent sessions.
                            termination_message_policy="FallbackToLogsOnError",
                            ports=[
                                k8s.ContainerPort(name="smtp", container_port=_SMTP_PORT),
                                k8s.ContainerPort(name="http", container_port=_HTTP_PORT),
                                k8s.ContainerPort(name="imap", container_port=_IMAP_PORT),
                            ],
                            env=[_db_password_env(), public_url],
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
                            config_map=k8s.ConfigMapVolumeSource(name="haku-mailbox-config", default_mode=0o555),
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
            name="haku-mailbox-smtp",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Cluster-internal SMTP backend for the per-node haku-mailbox-smtp-ingress proxies. "
                    "The proxies carry the sending MTA address in PROXY protocol so Stalwart can "
                    "evaluate SPF against the real peer."
                )
            },
        ),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[k8s.ServicePort(name="smtp", port=_SMTP_PORT, target_port=k8s.IntOrString.from_string("smtp"))],
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(
            name=NAME,
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
            selector=_LABELS,
            ports=[
                k8s.ServicePort(name="http", port=_HTTP_PORT, target_port=k8s.IntOrString.from_string("http")),
                k8s.ServicePort(name="imap", port=_IMAP_PORT, target_port=k8s.IntOrString.from_string("imap")),
            ],
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
            name=_INGRESS_NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Per-public-node port-25 TCP ingress. Preserves the sending MTA address through "
                    "PROXY protocol so Stalwart's SPF gate remains meaningful."
                ),
                "reloader.stakater.com/auto": "true",
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
                    tolerations=[
                        k8s.Toleration(
                            key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                        )
                    ],
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
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_INGRESS_NAME)),
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
    cilium.network_policy(
        chart,
        "smtp-ingress-policy",
        metadata=metadata(_INGRESS_NAME, NAMESPACE),
        selector=_INGRESS_LABELS,
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
        egress=[cilium.dns_egress(), cilium.egress_to(_LABELS, _SMTP_PORT)],
    )
    cilium.network_policy(
        chart,
        "policy",
        metadata=metadata(NAME, NAMESPACE),
        selector=_LABELS,
        ingress=[
            cilium.ingress_from(_INGRESS_LABELS, ports=[_SMTP_PORT]),
            cilium.ingress_from_gateway(_HTTP_PORT),
            cilium.ingress_from({"k8s:io.kubernetes.pod.namespace": namespace.NAMESPACE}, ports=[_IMAP_PORT]),
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
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "name": NAMESPACE,
                # The per-public-node SMTP ingress must bind hostPort 25. Pod Security's baseline
                # profile forbids every hostPort, so this trusted, operator-only namespace needs
                # privileged admission even though its workloads retain restrictive container
                # security contexts and Cilium policies.
                "pod-security.kubernetes.io/enforce": "privileged",
            },
        ),
    )
    _add_store(chart)
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    Certificate(
        chart,
        "certificate",
        metadata=metadata(
            "mx-allegedly-works",
            NAMESPACE,
            annotations={
                "description": (
                    "STARTTLS certificate for the inbound SMTP listener (mx.allegedly.works). Sending MTAs "
                    "(Gmail) use opportunistic TLS; the reloader annotation on the deployment restarts the "
                    "receiver when cert-manager rotates this."
                )
            },
        ),
        spec=CertificateSpec(
            secret_name=_TLS_SECRET,
            dns_names=["mx.allegedly.works"],
            issuer_ref=CertificateSpecIssuerRef(name="${LETSENCRYPT_ISSUER}", kind="ClusterIssuer"),
        ),
    )
    _add_deployment(chart)
    _add_services(chart)
    _add_smtp_ingress(chart)
    gateway.https_route(
        chart,
        "route",
        metadata=metadata(
            NAME,
            NAMESPACE,
            annotations={
                "description": (
                    "Public route to Stalwart's HTTP listener: JMAP for haku (authenticated with its "
                    "Authentik-issued bearer, validated by the server's OIDC directory) plus the "
                    "management API/WebUI (admin credential only, which haku cannot read). SMTP (:25) "
                    "enters through the per-node TCP ingress DaemonSet, not the HTTP gateway."
                )
            },
        ),
        hostname="haku-mailbox.allegedly.works",
        backend=NAME,
        port=_HTTP_PORT,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    # Mirror the rotator-published mailbox JWT into haku-sandbox, where Haku reads it to
    # authenticate to its mailbox over JMAP (base/sources/mailbox.md). Same ESO pattern as the
    # grocy-sf token (haku/managed-agent/grocy-token-eso.yaml): it sources the flux-system Secret
    # the rotator writes (k8s_secret output) and refreshes continuously, so the rotated token
    # propagates without any rotator-side distribution config.
    ClusterExternalSecret(
        chart,
        "mail-token",
        metadata=ApiObjectMetadata(name="haku-mail-token"),
        spec=ClusterExternalSecretSpec(
            namespaces=[namespace.NAMESPACE],
            external_secret_spec=ClusterExternalSecretSpecExternalSecretSpec(
                refresh_interval="1m",
                secret_store_ref=ClusterExternalSecretSpecExternalSecretSpecSecretStoreRef(
                    name="kubernetes-flux-system-secret-store",
                    kind=ClusterExternalSecretSpecExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
                ),
                target=ClusterExternalSecretSpecExternalSecretSpecTarget(name="haku-mail-token"),
                data=[
                    ClusterExternalSecretSpecExternalSecretSpecData(
                        secret_key="jwt",
                        remote_ref=ClusterExternalSecretSpecExternalSecretSpecDataRemoteRef(
                            key="haku-mail-token", property="jwt"
                        ),
                    )
                ],
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
