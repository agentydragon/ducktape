"""The allegedly.works static site: nginx serving one inline page on the shared Gateway."""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.gateway import cluster_gateway_parent_ref
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/website"
_NAME = "website"
_NAMESPACE = "website"
_LABELS = {"app.kubernetes.io/name": _NAME}
_CONTENT_CONFIG_MAP = "website-content"
_PORT = 8080

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
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={"goldilocks.fairwinds.com/enabled": "true", "goldilocks.fairwinds.com/vpa-update-mode": "auto"},
        ),
    )
    k8s.KubeConfigMap(
        chart,
        "content",
        metadata=k8s.ObjectMeta(name=_CONTENT_CONFIG_MAP, namespace=_NAMESPACE),
        data={"index.html": _INDEX_HTML},
    )
    probe_action = k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_number(_PORT))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels=_LABELS),
        spec=k8s.DeploymentSpec(
            replicas=2,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}),
                spec=k8s.PodSpec(
                    containers=[
                        k8s.Container(
                            name="nginx",
                            image="nginxinc/nginx-unprivileged:1.31-alpine",
                            ports=[k8s.ContainerPort(container_port=_PORT)],
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
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(name="http", port=80, target_port=k8s.IntOrString.from_number(_PORT), protocol="TCP")
            ],
            type="ClusterIP",
        ),
    )
    HttpRoute(
        chart,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref()],
            hostnames=["www.allegedly.works", "allegedly.works"],
            rules=[HttpRouteSpecRules(backend_refs=[HttpRouteSpecRulesBackendRefs(name=_NAME, port=80)])],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def website(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, gateway: Kustomization) -> Kustomization:
    name = "website"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            depends_on=[
                # TLS is owned by the shared Gateway; Website only supplies an HTTPRoute.
                flux_kustomization_depends_on(gateway)
            ],
        ),
    )
