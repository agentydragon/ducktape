"""External Helm chart dependencies (subcharts for cluster charts)."""

load("@rules_helm//helm:defs.bzl", "helm_import_repository")

def helm_chart_deps():
    """Import external Helm charts used as subchart dependencies."""
    helm_import_repository(
        name = "helm_charts__postgresql",
        repository = "https://charts.bitnami.com/bitnami",
        chart_name = "postgresql",
        version = "18.1.13",
    )

    helm_import_repository(
        name = "helm_charts__mariadb",
        url = "oci://registry-1.docker.io/bitnamicharts/mariadb:23.2.2",
    )
