"""Visual tests that render Grafana dashboards against synthetic metrics."""

load("//devinfra/python:defs.bzl", "py_test")

def grafana_dashboard_visual_test(name, dashboard, src = "//grafana:dashboard_visual_test.py", fixture = None, fixture_data = None, deps = []):
    """Declare a dashboard-specific screenshot scenario and Prometheus fixture.

    The default scenario uses shared synthetic rendering data. An optional custom
    scenario can import its fixture and exercise real datasource queries instead.
    """
    env = {"GRAFANA_DASHBOARD_RLOCATION": "$(rlocationpath %s)" % dashboard}
    if fixture_data:
        env["GRAFANA_FIXTURE_RLOCATION"] = "$(rlocationpath %s)" % fixture_data

    py_test(
        name = name,
        srcs = [src],
        main = src,
        data = [dashboard] + ([fixture_data] if fixture_data else []),
        env = env,
        requires_docker = True,
        tags = ["visual"],
        deps = ([fixture] if fixture else []) + [
            "//grafana:synthetic",
            "//grafana:visual_browser",
            "//grafana:visual_harness",
            "//util/bazel:runfiles",
            "//util/testing:frontend_visual",
            "//util/testing:undeclared_outputs",
            "//util/testing:visual_capture",
            "@pypi//playwright",
            "@pypi//pytest",
            "@pypi//pytest_asyncio",
            "@pypi//pytest_bazel",
        ] + deps,
    )
