"""The allegedly.works static site: nginx serving one inline page on the shared Gateway."""

from __future__ import annotations

import textwrap

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{GENERATED_ROOT}/website"
_NAME = "website"
_NAMESPACE = "website"
_CONTENT_CONFIG_MAP = "website-content"
HOSTNAME = "www.allegedly.works"
# nginx-unprivileged listens on 8080.
_SERVICE = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=80),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
    target_port=8080,
)

_INDEX_HTML = textwrap.dedent(
    """\
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>agentydragon.com</title>
        <style>
            :root {
                --bg: #1a1a2e;
                --text: #eaeaea;
                --accent: #e94560;
                --secondary: #16213e;
            }
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: var(--bg);
                color: var(--text);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 2rem;
            }
            .container {
                max-width: 600px;
                text-align: center;
            }
            .dragon {
                font-size: 4rem;
                margin-bottom: 1rem;
            }
            h1 {
                font-size: 2.5rem;
                margin-bottom: 0.5rem;
                color: var(--accent);
            }
            .tagline {
                color: #888;
                margin-bottom: 2rem;
                font-style: italic;
            }
            .status {
                background: var(--secondary);
                padding: 1.5rem;
                border-radius: 8px;
                margin-bottom: 2rem;
            }
            .status h2 {
                color: var(--accent);
                margin-bottom: 0.5rem;
                font-size: 1rem;
            }
            .status p {
                color: #aaa;
                font-size: 0.9rem;
            }
            .links {
                display: flex;
                gap: 1rem;
                justify-content: center;
                flex-wrap: wrap;
            }
            .links a {
                color: var(--accent);
                text-decoration: none;
                padding: 0.5rem 1rem;
                border: 1px solid var(--accent);
                border-radius: 4px;
                transition: all 0.2s;
            }
            .links a:hover {
                background: var(--accent);
                color: var(--bg);
            }
            footer {
                margin-top: 3rem;
                color: #555;
                font-size: 0.8rem;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="dragon">🐉</div>
            <h1>agentydragon.com</h1>
            <p class="tagline">Infrastructure under construction</p>

            <div class="status">
                <h2>Migration in Progress</h2>
                <p>This site is being migrated to a new Kubernetes cluster.</p>
                <p>The real content will return soon.</p>
            </div>

            <div class="links">
                <a href="https://github.com/agentydragon">GitHub</a>
                <a href="https://gitlab.com/agentydragon">GitLab</a>
            </div>
        </div>
        <footer>
            Served from Talos Kubernetes cluster
        </footer>
    </body>
    </html>
"""
)


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO)
    k8s.KubeConfigMap(
        chart,
        "content",
        metadata=k8s.ObjectMeta(name=_CONTENT_CONFIG_MAP, namespace=_NAMESPACE),
        data={"index.html": _INDEX_HTML},
    )
    probe_action = k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_number(_SERVICE.pod_port))
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
                    automount_service_account_token=False,
                    containers=[
                        k8s.Container(
                            name="nginx",
                            image="nginxinc/nginx-unprivileged:1.31-alpine",
                            ports=[k8s.ContainerPort(container_port=_SERVICE.pod_port)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("16Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    # TODO(vpa-memory-audit): 64Mi -> 192Mi. VPA observed 100Mi for
                                    # both request and upper bound. That is a lot for nginx serving
                                    # static files; likely worker count x buffer sizing rather than
                                    # real need, so this ceiling should come back down.
                                    "memory": k8s.Quantity.from_string("192Mi"),
                                },
                            ),
                            volume_mounts=[k8s.VolumeMount(name="html", mount_path="/usr/share/nginx/html")],
                            liveness_probe=k8s.Probe(http_get=probe_action, initial_delay_seconds=5, period_seconds=10),
                            readiness_probe=k8s.Probe(http_get=probe_action, initial_delay_seconds=2, period_seconds=5),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"], add=["NET_BIND_SERVICE"]),
                                read_only_root_filesystem=False,
                                run_as_non_root=False,
                            ),
                        )
                    ],
                    volumes=[k8s.Volume(name="html", config_map=k8s.ConfigMapVolumeSource(name=_CONTENT_CONFIG_MAP))],
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


def website(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    name = "website"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="5m",
        # Kyverno's failurePolicy: Fail webhooks admit the Deployment and HTTPRoute.
        depends_on=[flux_kustomization_depends_on(kyverno)],
    )
