"""Visual tests that render Grafana dashboards with explicit fixtures."""

load("//devinfra/python:defs.bzl", "py_test")

def grafana_dashboard_visual_test(name, dashboard, src = "//grafana/visual_tests:dashboard_visual_test.py", fixture = None, fixture_data = None, deps = []):
    """Declare a dashboard-specific screenshot scenario and fixture.

    The default scenario replays dashboard-owned data. An optional custom
    scenario can import its fixture and exercise real datasource queries instead.
    """
    if src == "//grafana/visual_tests:dashboard_visual_test.py" and not fixture_data:
        fail("the default Grafana visual scenario requires dashboard fixture_data")
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
            "//grafana/visual_tests:recorded",
            "//grafana/visual_tests:visual_browser",
            "//grafana/visual_tests:visual_harness",
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
