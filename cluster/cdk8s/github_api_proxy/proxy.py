"""The central GitHub API proxy: its Namespace and Certificates (`identity/`), and the proxy
workload with its capture storage, Service and network policy (`app/`).

The proxy image tag is the placeholder "unset"; the hand-written
cluster/k8s/github-api-proxy/app/image-pins/kustomization.yaml overrides it at `kustomize build`
time via Flux's image-automation marker. Also hand-written in `app/`: the dedicated Gateway and
TLSRoute, the PodMonitor and PrometheusRule (no typed bindings for those kinds yet), the client
credential SOPS Secrets, `config.json` (rendered by the `configMapGenerator`) and that
`kustomization.yaml`. The Flux Kustomization substitutes `${LETSENCRYPT_ISSUER}`
(`cert-manager-issuer-config`) into the server Certificate.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecUsages,
)
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEgressDeny,
    CiliumNetworkPolicySpecEgressDenyToPorts,
    CiliumNetworkPolicySpecEgressDenyToPortsPorts,
    CiliumNetworkPolicySpecEgressDenyToPortsPortsProtocol,
)
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

_IDENTITY_DIR = "cluster/k8s/github-api-proxy/identity"
_APP_DIR = "cluster/k8s/github-api-proxy/app"
_NAME = "github-api-proxy"
_NAMESPACE = "github-api-proxy"
_LABELS = {"app.kubernetes.io/name": _NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/github-api-proxy:unset"
_HOSTNAME = "github-proxy.allegedly.works"
_SERVER_TLS_SECRET = "github-api-proxy-server-tls"
_INTERCEPTION_CA = "github-api-proxy-interception-ca"
_CAPTURE_CLAIM = "github-api-proxy-capture"
_PROXY_PORT = 8080
_METRICS_PORT = 9090
_RUN_DIR = "/run/github-api-proxy"
_CLIENTS = ("wyrm2", "rugged")


def _namespace(scope: Construct) -> None:
    k8s.KubeNamespace(
        scope,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "off",
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/warn": "restricted",
            },
        ),
    )


def _certificates(scope: Construct) -> None:
    Certificate(
        scope,
        "server",
        metadata=metadata("github-api-proxy-server", _NAMESPACE),
        spec=CertificateSpec(
            secret_name=_SERVER_TLS_SECRET,
            dns_names=[_HOSTNAME],
            private_key=CertificateSpecPrivateKey(
                algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA,
                size=256,
                rotation_policy=CertificateSpecPrivateKeyRotationPolicy.ALWAYS,
            ),
            usages=[CertificateSpecUsages.SERVER_AUTH],
            issuer_ref=CertificateSpecIssuerRef(name="${LETSENCRYPT_ISSUER}", kind="ClusterIssuer"),
        ),
    )
    Certificate(
        scope,
        "interception-ca",
        metadata=metadata(
            _INTERCEPTION_CA,
            _NAMESPACE,
            annotations={
                "description": (
                    "Dedicated workstation proxy interception root. Only its public certificate may be "
                    "distributed to clients; the signing key stays in this namespace."
                )
            },
        ),
        spec=CertificateSpec(
            is_ca=True,
            common_name="ducktape-github-api-proxy-interception-ca",
            secret_name=_INTERCEPTION_CA,
            duration="87600h",
            renew_before="8760h",
            private_key=CertificateSpecPrivateKey(
                algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA,
                size=256,
                # A signing-key rotation requires an explicit client trust migration.
                rotation_policy=CertificateSpecPrivateKeyRotationPolicy.NEVER,
            ),
            usages=[CertificateSpecUsages.CERT_SIGN, CertificateSpecUsages.CRL_SIGN],
            issuer_ref=CertificateSpecIssuerRef(name="cluster-ca-bootstrap", kind="ClusterIssuer"),
        ),
    )


def identity_chart(app: App) -> Chart:
    chart = Chart(app, f"{_NAME}-identity", disable_resource_name_hashes=True)
    _namespace(chart)
    _certificates(chart)
    return chart


def _capture_claim(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "capture",
        metadata=k8s.ObjectMeta(
            name=_CAPTURE_CLAIM,
            namespace=_NAMESPACE,
            annotations={
                "kustomize.toolkit.fluxcd.io/prune": "disabled",
                "description": (
                    "Private investigation evidence. Retain on app removal; deletion requires explicit "
                    "evidence-retention review."
                ),
            },
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteMany"],
            storage_class_name="seaweedfs-ovh",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("100Gi")}),
        ),
    )


def _proxy_container() -> k8s.Container:
    return k8s.Container(
        name="proxy",
        image=_IMAGE,
        image_pull_policy="IfNotPresent",
        args=["--config", f"{_RUN_DIR}/config/config.json"],
        ports=[
            k8s.ContainerPort(name="proxy", container_port=_PROXY_PORT, protocol="TCP"),
            k8s.ContainerPort(name="metrics", container_port=_METRICS_PORT, protocol="TCP"),
        ],
        security_context=k8s.SecurityContext(
            allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
        ),
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("100m"), "memory": k8s.Quantity.from_string("256Mi")},
            limits={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("2Gi")},
        ),
        readiness_probe=k8s.Probe(
            http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string("metrics")),
            initial_delay_seconds=3,
            period_seconds=5,
            timeout_seconds=2,
        ),
        volume_mounts=[
            k8s.VolumeMount(name="config", mount_path=f"{_RUN_DIR}/config", read_only=True),
            k8s.VolumeMount(name="clients", mount_path=f"{_RUN_DIR}/clients", read_only=True),
            k8s.VolumeMount(name="outer-tls", mount_path=f"{_RUN_DIR}/outer-tls", read_only=True),
            k8s.VolumeMount(name="interception-ca", mount_path=f"{_RUN_DIR}/interception-ca", read_only=True),
            k8s.VolumeMount(name="private-work", mount_path=f"{_RUN_DIR}/work"),
            k8s.VolumeMount(name="capture", mount_path="/var/lib/github-api-proxy"),
        ],
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME, namespace=_NAMESPACE, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    # No explicit zone nodeSelector: github-api-proxy-capture's seaweedfs-ovh
                    # StorageClass already pins scheduling to topology.kubernetes.io/zone=hil-ovh
                    # via WaitForFirstConsumer + allowedTopologies
                    # (cluster/k8s/seaweedfs-csi/sc-seaweedfs-ovh.yaml).
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    automount_service_account_token=False,
                    termination_grace_period_seconds=60,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        fs_group=1000,
                        fs_group_change_policy="OnRootMismatch",
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[_proxy_container()],
                    volumes=[
                        # Rendered from config.json by the hand-written kustomization.yaml's
                        # configMapGenerator.
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name="github-api-proxy-config")),
                        k8s.Volume(
                            name="clients",
                            projected=k8s.ProjectedVolumeSource(
                                default_mode=0o440,
                                sources=[
                                    k8s.VolumeProjection(
                                        secret=k8s.SecretProjection(
                                            name=f"github-api-proxy-{client}-credentials",
                                            items=[
                                                k8s.KeyToPath(key=key, path=f"{client}/{key}")
                                                for key in ("username", "password")
                                            ],
                                        )
                                    )
                                    for client in _CLIENTS
                                ],
                            ),
                        ),
                        k8s.Volume(
                            name="outer-tls",
                            secret=k8s.SecretVolumeSource(secret_name=_SERVER_TLS_SECRET, default_mode=0o440),
                        ),
                        k8s.Volume(
                            name="interception-ca",
                            secret=k8s.SecretVolumeSource(secret_name=_INTERCEPTION_CA, default_mode=0o440),
                        ),
                        k8s.Volume(
                            name="private-work",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("16Mi")
                            ),
                        ),
                        k8s.Volume(
                            name="capture",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_CAPTURE_CLAIM),
                        ),
                    ],
                ),
            ),
        ),
    )


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="proxy-tls", port=443, target_port=k8s.IntOrString.from_string("proxy"), protocol="TCP"
                )
            ],
        ),
    )


def _network_policy(scope: Construct) -> None:
    cilium.network_policy(
        scope,
        "network-policy",
        metadata=metadata(_NAME, _NAMESPACE),
        selector=_LABELS,
        ingress=[
            cilium.ingress_from_gateway(_PROXY_PORT),
            cilium.ingress_from(cilium.endpoint_labels("monitoring", "alloy"), ports=[_METRICS_PORT]),
        ],
        egress=[
            cilium.dns_egress(protocols=["ANY"], resolves=["*"]),
            cilium.egress_to_entities("world", ports=[80, 443]),
        ],
        # These non-public ranges can be outside Cilium's cluster identity set.
        # Limit the deny to web ports so the explicit cluster-DNS exception remains.
        # Runtime destination validation also fences loopback and DNS rebinding.
        egress_deny=[
            CiliumNetworkPolicySpecEgressDeny(
                to_cidr=[
                    "0.0.0.0/8",
                    "10.0.0.0/8",
                    "100.64.0.0/10",
                    "127.0.0.0/8",
                    "169.254.0.0/16",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "::/128",
                    "::1/128",
                    "fc00::/7",
                    "fe80::/10",
                ],
                to_ports=[
                    CiliumNetworkPolicySpecEgressDenyToPorts(
                        ports=[
                            CiliumNetworkPolicySpecEgressDenyToPortsPorts(
                                port=str(port), protocol=CiliumNetworkPolicySpecEgressDenyToPortsPortsProtocol.TCP
                            )
                            for port in (80, 443)
                        ]
                    )
                ],
            )
        ],
    )


def app_chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    _capture_claim(chart)
    _deployment(chart)
    _network_policy(chart)
    _service(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _IDENTITY_DIR, identity_chart)
    write_yaml(
        root / _IDENTITY_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{_NAME}-identity.k8s.yaml"])
    )
    write_charts(root, _APP_DIR, app_chart)
