"""One Python test module per target, sharing the Agentplane browser assets."""

load("//testing/frontend_visual:py_visual_test.bzl", "py_visual_test")

def agentplane_visual_test(name, src, title, shard_count = 1, test_deps = []):
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
        test_deps = test_deps + [
            "//agentplane/app/frontend/testing:app",
            "//agentplane/app/frontend/testing:visual_fixtures",
            "//agentplane/app/frontend/testing:visual_assertions",
            "//testing/frontend_visual:page_capture",
            "//testing/frontend_visual:viewports",
            "//testing/frontend_visual:capture",
            "@pypi//playwright",
            "@pypi//pytest",
            "@pypi//pytest_asyncio",
            "@pypi//pytest_bazel",
        ],
        test_module = "agentplane.app.frontend." + src.removesuffix(".py"),
        test_srcs = [src],
        title = title,
    )
