"""External Helm chart dependencies (subcharts for cluster charts)."""

load("@rules_helm//helm:defs.bzl", "helm_import_repository")

def helm_chart_deps():
    """Import external Helm charts used as subchart dependencies."""
    helm_import_repository(
        name = "helm_charts__postgresql",
        url = "oci://registry-1.docker.io/bitnamicharts/postgresql:18.1.13",
        sha256 = "b41bdf8c6f7a376b9762cedd0e97086193ac02d5eddb721e181e77e3c7047701",
    )

    helm_import_repository(
        name = "helm_charts__mariadb",
        url = "oci://registry-1.docker.io/bitnamicharts/mariadb:23.2.2",
        sha256 = "03170907d132d89710e0e74b43353ae3af43526e93e8648ec654789890ed5787",
    )
