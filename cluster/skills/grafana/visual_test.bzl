"""Visual tests that render Grafana dashboards against synthetic metrics."""

load("//devinfra/python:defs.bzl", "py_test")

def grafana_dashboard_visual_test(name, src, dashboard, fixture, deps = []):
    """Declare a dashboard-specific screenshot scenario and Prometheus fixture.

    The scenario in src owns its interactions and screenshot names. The fixture
    is a Python library supplying synthetic metrics. The scenario imports it and
    uses //cluster/skills/grafana:visual_harness to start the isolated services.
    """
    py_test(
        name = name,
        srcs = [src],
        main = src,
        data = [dashboard],
        env = {"GRAFANA_DASHBOARD_RLOCATION": "$(rlocationpath %s)" % dashboard},
        requires_docker = True,
        tags = ["visual"],
        deps = [
            fixture,
            "//cluster/skills/grafana:visual_harness",
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
