"""Disposable namespace and ESO-owned pull secret for the launcher experiment."""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount, k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetTemplate

from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef


def setup(scope: Construct, namespace: str) -> None:
    if not namespace.startswith("agentplane-vm-prototype-"):
        raise ValueError("Prototype namespace must start with agentplane-vm-prototype-")
    k8s.KubeNamespace(scope, "namespace", metadata=k8s.ObjectMeta(name=namespace))
    k8s.KubeResourceQuota(
        scope,
        "quota",
        metadata=k8s.ObjectMeta(name="prototype", namespace=namespace),
        spec=k8s.ResourceQuotaSpec(
            hard={
                "pods": k8s.Quantity.from_number(8),
                "requests.memory": k8s.Quantity.from_string("8Gi"),
                "requests.cpu": k8s.Quantity.from_number(4),
                "count/virtualmachines.kubevirt.io": k8s.Quantity.from_number(2),
            }
        ),
    )
    reader = ServiceAccount(scope, "pull-reader", metadata=ApiObjectMetadata(name="pull-reader", namespace=namespace))
    store = single_secret_store(
        scope,
        namespace,
        reader=reader,
        source_namespace="forgejo-images",
        source_secret="forgejo-images-creds",
        consumer_namespace=namespace,
    )
    ExternalSecret(
        scope,
        "pull-secret",
        metadata=ApiObjectMetadata(name="forgejo-images-creds", namespace=namespace),
        secret_store_ref=SecretStoreRef.cluster(store),
        data_from=[DataFrom.from_extract("forgejo-images-creds")],
        template=ExternalSecretSpecTargetTemplate(type="kubernetes.io/dockerconfigjson"),
    )


def gateway(scope: Construct, namespace: str, source: str) -> None:
    """TokenReview-only synthetic endpoint; its account can review, not mint tokens."""
    labels = {"app": "prototype-gateway"}
    k8s.KubeServiceAccount(
        scope, "gateway-account", metadata=k8s.ObjectMeta(name="prototype-gateway", namespace=namespace)
    )
    k8s.KubeClusterRole(
        scope,
        "review-role",
        metadata=k8s.ObjectMeta(name=f"{namespace}-reviewer"),
        rules=[k8s.PolicyRule(api_groups=["authentication.k8s.io"], resources=["tokenreviews"], verbs=["create"])],
    )
    k8s.KubeClusterRoleBinding(
        scope,
        "review-binding",
        metadata=k8s.ObjectMeta(name=f"{namespace}-reviewer"),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=f"{namespace}-reviewer"),
        subjects=[k8s.Subject(kind="ServiceAccount", name="prototype-gateway", namespace=namespace)],
    )
    k8s.KubeConfigMap(
        scope,
        "gateway-code",
        metadata=k8s.ObjectMeta(name="prototype-gateway", namespace=namespace),
        data={"gateway.py": source},
    )
    k8s.KubePod(
        scope,
        "gateway",
        metadata=k8s.ObjectMeta(name="prototype-gateway", namespace=namespace, labels=labels),
        spec=k8s.PodSpec(
            service_account_name="prototype-gateway",
            containers=[
                k8s.Container(
                    name="gateway",
                    image="python:3.13-alpine",
                    command=["python", "/app/gateway.py"],
                    security_context=k8s.SecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        allow_privilege_escalation=False,
                        capabilities=k8s.Capabilities(drop=["ALL"]),
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    resources=k8s.ResourceRequirements(
                        requests={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string("32Mi")},
                        limits={"memory": k8s.Quantity.from_string("128Mi")},
                    ),
                    volume_mounts=[k8s.VolumeMount(name="code", mount_path="/app", read_only=True)],
                )
            ],
            volumes=[k8s.Volume(name="code", config_map=k8s.ConfigMapVolumeSource(name="prototype-gateway"))],
        ),
    )
    k8s.KubeService(
        scope,
        "gateway-service",
        metadata=k8s.ObjectMeta(name="prototype-gateway", namespace=namespace),
        spec=k8s.ServiceSpec(selector=labels, ports=[k8s.ServicePort(port=8888)]),
    )
    k8s.KubeNetworkPolicy(
        scope,
        "launcher-fence",
        metadata=k8s.ObjectMeta(name="launcher-fence", namespace=namespace),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels={"kubevirt.io": "virt-launcher"}),
            policy_types=["Ingress", "Egress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[k8s.NetworkPolicyPeer(pod_selector=k8s.LabelSelector(match_labels=labels))],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(7000), protocol="TCP")],
                )
            ],
            egress=[
                k8s.NetworkPolicyEgressRule(
                    to=[k8s.NetworkPolicyPeer(pod_selector=k8s.LabelSelector(match_labels=labels))],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(8888), protocol="TCP")],
                ),
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
                        k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(53), protocol=p) for p in ["TCP", "UDP"]
                    ],
                ),
            ],
        ),
    )
