"""The homepage (website/, rendered by Hakyll into the image) on the shared Gateway."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.chart import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{GENERATED_ROOT}/website"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/website-image-pins"
_NAME = "website"
_NAMESPACE = "website"
HOSTNAME = "www.allegedly.works"
_SERVICE = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=80),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
    target_port=8080,
)

# The PINS_DIR Component sets the real tag.
_IMAGE = "git.allegedly.works/ducktape-ci/website:unset"


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    probe_action = k8s.HttpGetAction(path="/health", port=k8s.IntOrString.from_number(_SERVICE.pod_port))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels=_SERVICE.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=2,
            selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    containers=[
                        k8s.Container(
                            name="server",
                            image=_IMAGE,
                            ports=[k8s.ContainerPort(container_port=_SERVICE.pod_port)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("16Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                            ),
                            liveness_probe=k8s.Probe(http_get=probe_action, initial_delay_seconds=5, period_seconds=10),
                            readiness_probe=k8s.Probe(http_get=probe_action, initial_delay_seconds=2, period_seconds=5),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                            ),
                        )
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_SERVICE.name, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_SERVICE.pods.selector,
            ports=[
                k8s.ServicePort(
                    name=_SERVICE.port.name,
                    port=_SERVICE.port.number,
                    target_port=k8s.IntOrString.from_number(_SERVICE.pod_port),
                    protocol="TCP",
                )
            ],
            type="ClusterIP",
        ),
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        hostnames=[HOSTNAME, "allegedly.works"],
        backend=_SERVICE,
        hsts=False,
        listener=None,
    )
    return chart


def website(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization, kyverno: Kustomization
) -> Kustomization:
    name = "website"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="5m",
        # Kyverno's failurePolicy: Fail webhooks admit the Deployment and HTTPRoute.
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator, kyverno),
    )
