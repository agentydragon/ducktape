"""Bzlmod extension for external Helm chart dependencies."""

load("//cluster/charts:helm_deps.bzl", "helm_chart_deps")

def _helm_charts_impl(_ctx):
    helm_chart_deps()

helm_charts = module_extension(
    implementation = _helm_charts_impl,
)
