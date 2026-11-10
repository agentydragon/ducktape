"""haku-ci's door to Agentplane egress: a shared relay in the haku-ci namespace that presents one
standing SVID to the egress proxy for every runner.

A relay rather than a per-Pod sidecar because the build steps run inside rootless dind, whose
slirp4netns network cannot reach the runner Pod's loopback; job containers reach a cluster
Service, as they reached haku-egress-proxy. It also keeps the SVID's key out of the privileged
runner Pod: the Secret is mounted here alone. A standing record proves no Pod, so every replica is
the same caller (agentplane/docs/workload_authentication.md § Standing workloads), and whatever
reaches this Service acts as that caller: its ingress admits only the runner Pods.

Rendered by the agentplane-staging chart, whose image-pins Component pins the sidecar image to the
egress proxy release it talks to.
"""

from pathlib import Path

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount, k8s
from constructs import Construct

from agentplane.egress import sidecar_config
from cluster.cdk8s import node_scheduling
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.providers.agentplane.workload_credential import WorkloadCredential
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

NAME = "haku-ci-egress"
_PLACEHOLDER_TAG = "unset"  # always overridden by agentplane-staging's image-pins/kustomization.yaml
_SIDECAR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"
_SETTINGS_PATH = "/etc/agentplane-egress-sidecar/settings.yaml"
_SVID_DIR = Path("/var/run/agentplane-svid")
# The workload CA alone, which the egress proxy's server SVID chains to.
_WORKLOAD_CA_DIR = Path("/etc/agentplane-workload-ca")
_ZONES = node_scheduling.HOME_AND_OVH_ZONES


def service(namespace: str) -> ServiceRef:
    """The HTTP(S) proxy haku-ci's runners, dind and job containers name."""
    return ServiceRef(
        name=NAME,
        port=Port(name="proxy", number=3128),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", NAME),)),
    )


def add_gateway(scope: Construct, env: Environment, *, clients: Pods) -> None:
    """The relay for `clients` (the runner Pods), in their namespace, and its admission to the proxy."""
    namespace = clients.namespace
    gateway = service(namespace)
    proxy = egress.proxy(env.namespace)
    account = ServiceAccount(
        scope,
        "haku-ci-egress-account",
        metadata=ApiObjectMetadata(
            name=NAME,
            namespace=namespace,
            annotations={
                "description": "The subject haku-ci's builds reach the public internet as (PolicyBinding haku-ci-egress)."
            },
        ),
        automount_token=False,
    )
    WorkloadCredential(
        scope,
        "haku-ci-egress-workload-credential",
        metadata=ApiObjectMetadata(
            name=NAME,
            namespace=namespace,
            annotations={"description": "The SVID haku-ci's egress relay presents to the egress proxy."},
        ),
        subject=account,
    )
    forgejo_images_creds_external_secret(scope, "haku-ci-egress-pull-credentials", namespace=namespace)
    settings = SettingsFile(
        scope,
        "haku-ci-egress-settings",
        metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
        content=yaml_config(
            sidecar_config.Settings(
                proxy=sidecar_config.Proxy(
                    address=f"{proxy.fqdn}:{proxy.port.number}",
                    transport=sidecar_config.TLSProxyTransport(
                        ca_file=_WORKLOAD_CA_DIR / cluster_ca.BUNDLE_KEY,
                        auth=sidecar_config.SpiffeProxyAuth(
                            cert_file=_SVID_DIR / "tls.crt", key_file=_SVID_DIR / "tls.key"
                        ),
                    ),
                ),
                listen_address=f"0.0.0.0:{gateway.pod_port}",
            )
        ),
        path=_SETTINGS_PATH,
    ).pod_wiring(volume_name="settings", env=sidecar_config.Settings.config_file_env)
    k8s.KubeDeployment(
        scope,
        "haku-ci-egress-deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=namespace, labels=gateway.pods.selector),
        spec=k8s.DeploymentSpec(
            # One endpoint in OVH and each home zone; the Service prefers the runner's zone.
            replicas=len(_ZONES),
            strategy=k8s.DeploymentStrategy(
                type="RollingUpdate",
                rolling_update=k8s.RollingUpdateDeployment(
                    max_surge=k8s.IntOrString.from_number(1), max_unavailable=k8s.IntOrString.from_number(0)
                ),
            ),
            selector=k8s.LabelSelector(match_labels=gateway.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=gateway.pods.selector),
                spec=k8s.PodSpec(
                    service_account_name=account.name,
                    automount_service_account_token=False,
                    enable_service_links=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        # Lets the non-root sidecar read the 0440 SVID Secret.
                        fs_group=1000,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    topology_spread_constraints=[node_scheduling.zone_spread(_ZONES, gateway.pods.selector)],
                    affinity=k8s.Affinity(node_affinity=node_scheduling.zone_affinity(_ZONES)),
                    containers=[
                        k8s.Container(
                            name=sidecar_config.CONTAINER_NAME,
                            image=f"{_SIDECAR_IMAGE}:{_PLACEHOLDER_TAG}",
                            env=[settings.path_env],
                            ports=[gateway.port.k8s_container_port()],
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path=sidecar_config.READINESS_PATH,
                                    port=k8s.IntOrString.from_number(sidecar_config.READINESS_PORT),
                                ),
                                period_seconds=2,
                                timeout_seconds=1,
                                failure_threshold=1,
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                read_only_root_filesystem=True,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                            # Streams CI downloads (Bazel toolchains, image layers) without buffering bodies.
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                            volume_mounts=[
                                settings.mount,
                                k8s.VolumeMount(name="svid", mount_path=str(_SVID_DIR), read_only=True),
                                k8s.VolumeMount(name="workload-ca", mount_path=str(_WORKLOAD_CA_DIR), read_only=True),
                            ],
                        )
                    ],
                    volumes=[
                        settings.volume,
                        # The issuer's Secret for the record above; the gateway's whole identity at the proxy.
                        k8s.Volume(
                            name="svid", secret=k8s.SecretVolumeSource(secret_name=f"svid-{NAME}", default_mode=0o440)
                        ),
                        k8s.Volume(
                            name="workload-ca", config_map=k8s.ConfigMapVolumeSource(name=cluster_ca.WORKLOAD_BUNDLE)
                        ),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        scope,
        "haku-ci-egress-service",
        metadata=k8s.ObjectMeta(name=gateway.name, namespace=namespace, labels=gateway.labels),
        spec=k8s.ServiceSpec(
            selector=gateway.pods.selector, ports=[gateway.port.k8s_service_port()], traffic_distribution="PreferClose"
        ),
    )
    k8s.KubePodDisruptionBudget(
        scope,
        "haku-ci-egress-pdb",
        metadata=k8s.ObjectMeta(name=NAME, namespace=namespace),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(len(_ZONES) - 1),
            selector=k8s.LabelSelector(match_labels=gateway.pods.selector),
        ),
    )
    # Additive to haku-ci's force-proxy CCNP (haku_ci/runner.py), which already admits DNS.
    NetworkPolicy(
        scope,
        "haku-ci-egress-network",
        metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
        endpoint_selector=gateway.pods.selector,
        ingress=[clients.admit(gateway.pod_port)],
        egress=[proxy.egress()],
    )
    NetworkPolicy(
        scope,
        "haku-ci-runner-to-egress-network",
        metadata=ApiObjectMetadata(name=f"runner-to-{NAME}", namespace=namespace),
        endpoint_selector=clients.selector,
        egress=[gateway.egress()],
    )
    NetworkPolicy(
        scope,
        "haku-ci-egress-proxy-network",
        metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
        endpoint_selector=proxy.pods.selector,
        ingress=[gateway.pods.admit(proxy.pod_port)],
    )
