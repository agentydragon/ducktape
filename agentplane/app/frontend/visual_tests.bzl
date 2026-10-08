"""One Python test module per target, sharing the Agentplane browser assets."""

load("//util/testing/frontend_visual:py_visual_test.bzl", "py_visual_test")

def agentplane_visual_test(name, src, title, shard_count = 1):
    """Run one test file against the app's callable fixture harness."""
    py_visual_test(
        name = name,
        # Browser shards can exceed small's 60 seconds, including startup on loaded RBE workers.
        size = "medium",
        assets = [
            "harness/index.html",
            ":visual_bundle_css",
        ],
        # Retain the published phone images' fractional device-pixel geometry.
        devtools_viewport = True,
        harness = ":visual_bundle_js",
        shard_count = shard_count,
        test_deps = [
            ":visual_app",
            ":visual_fixtures",
            ":visual_assertions",
            "//util/testing:page_capture",
            "//util/testing:viewports",
            "//util/testing:visual_capture",
            "@pypi//playwright",
            "@pypi//pytest",
            "@pypi//pytest_asyncio",
            "@pypi//pytest_bazel",
        ],
        test_module = "agentplane.app.frontend." + src.removesuffix(".py"),
        test_srcs = [src],
        title = title,
    )
