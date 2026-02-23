"""External Helm chart dependencies (subcharts for cluster charts)."""

load("@rules_helm//helm:defs.bzl", "helm_import_repository")

def helm_chart_deps():
    """Import external Helm charts used as subchart dependencies."""
    helm_import_repository(
        name = "helm_charts__postgresql",
        url = "oci://registry-1.docker.io/bitnamicharts/postgresql:18.1.13",
        sha256 = "b41bdf8c6f7a376b9762cedd0e97086193ac02d5eddb721e181e77e3c7047701",
    )
