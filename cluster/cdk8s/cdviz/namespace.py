"""The namespace shared by CDviz's database and collector."""

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import namespaces
from cluster.cdk8s.cdviz import config
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.namespaces import Vpa

OUTPUT_DIR = f"{config.OUTPUT_DIR}/namespace"


def chart(app: App) -> Chart:
    chart = Chart(app, "cdviz-namespace", disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=config.NAMESPACE, vpa=Vpa.RECOMMEND)
    return chart


def cdviz_namespace(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(
        chart, "cdviz-namespace", directory, deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN
    )
