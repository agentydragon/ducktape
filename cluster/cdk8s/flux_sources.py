"""The upstream GitRepositories whose manifests Flux Kustomizations apply directly: the
CRDs of external-secrets, the Prometheus Operator, the CSI external-snapshotter and
sshpiper.

`cluster/k8s/flux/sources` is applied by the bootstrap `flux-system` Kustomization, not by
a node in the generated Flux chart.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from flux_gitrepository_crds.io.fluxcd.toolkit.source import GitRepositorySpecRef

from cluster.cdk8s.flux import NAMESPACE
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.flux.git_repository import GitRepository

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/flux/sources"


def _source(
    chart: Chart, name: str, *, url: str, tag: str, ignore: str | None = None, description: str | None = None
) -> GitRepository:
    return GitRepository(
        chart,
        name,
        metadata=ApiObjectMetadata(
            name=name, namespace=NAMESPACE, annotations={"description": description} if description else None
        ),
        interval="1h",
        url=url,
        ref=GitRepositorySpecRef(tag=tag),
        ignore=ignore,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, "git-repositories", disable_resource_name_hashes=True)
    _source(
        chart, "external-secrets-source", url="https://github.com/external-secrets/external-secrets.git", tag="v2.10.0"
    )
    _source(
        chart,
        "prometheus-operator-source",
        url="https://github.com/prometheus-operator/prometheus-operator.git",
        tag="v0.94.0",
        description="Prometheus Operator CRDs, split out of the kube-prometheus-stack HelmRelease so a "
        "ServiceMonitor's dependency is the CRD and not Prometheus being healthy. The tag tracks "
        "kube-prometheus-stack's appVersion - bump both together.",
    )
    _source(
        chart,
        "external-snapshotter-source",
        url="https://github.com/kubernetes-csi/external-snapshotter.git",
        tag="v8.6.0",
    )
    _source(
        chart,
        "sshpiper-source",
        url="https://github.com/tg123/sshpiper.git",
        # Match the sshpiperd image tag in public_coder/sshpiper.py.
        tag="v1.6.1",
        # Keep only the CRD. In particular, plugin/kubernetes/sample.yaml is an example
        # Pipe, not a production route to apply. Flux generates a kustomization.yaml for
        # this plain-YAML path.
        ignore=textwrap.dedent(
            """\
            /*
            !/plugin
            /plugin/*
            !/plugin/kubernetes
            /plugin/kubernetes/*
            !/plugin/kubernetes/crd.yaml
            """
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
