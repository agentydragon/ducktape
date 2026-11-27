"""The `grafana` HelmRepository that loki, mimir, tempo and alloy install from."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.flux import CONTROLLER_NAMESPACE
from cluster.cdk8s.helm import helm_repository_source_ref, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/grafana-helmrepository"
_NAME = "grafana"
# The releases installing from this repository live in other charts.
SOURCE_REF = helm_repository_source_ref(_NAME, CONTROLLER_NAMESPACE)


def chart(app: App) -> Chart:
    chart = Chart(app, "helmrepository", disable_resource_name_hashes=True)
    https_helm_repository(
        chart, _NAME, CONTROLLER_NAMESPACE, url="https://grafana.github.io/helm-charts", interval="12h"
    )
    return chart
