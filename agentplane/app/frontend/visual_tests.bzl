"""One Python test module per target, using an Agentplane browser harness."""

load("@aspect_rules_esbuild//esbuild:defs.bzl", "esbuild")
load("//testing/frontend_visual:py_visual_test.bzl", "py_visual_test")

def agentplane_visual_bundle(name, entry_point, output_dir, deps):
    """Build the classic script and stylesheet loaded by the file:// harness page."""
    script = output_dir + "/bundle.js"
    stylesheet = output_dir + "/bundle.css"
    esbuild(
        name = name,
        testonly = True,
        config = {"jsx": "automatic"},
        entry_point = entry_point,
        format = "iife",
        minify = False,
        output = script,
        output_css = stylesheet,
        platform = "browser",
        sourcemap = "linked",
        target = ["es2022"],
        deps = deps,
    )

    # Single-file views for runfiles path expansion.
    native.filegroup(name = name + "_js", testonly = True, srcs = [script])
    native.filegroup(name = name + "_css", testonly = True, srcs = [stylesheet])

def agentplane_visual_test(
        name,
        src,
        title,
        shard_count = 1,
        test_deps = [],
        bundle = ":visual_bundle",
        index = "harness/index.html"):
    """Run one test file against the app's callable fixture harness."""
    py_visual_test(
        name = name,
        # Browser shards can exceed small's 60 seconds, including startup on loaded RBE workers.
        size = "medium",
        assets = [
            index,
            bundle + "_css",
        ],
        # Retain the published phone images' fractional device-pixel geometry.
        devtools_viewport = True,
        harness = bundle + "_js",
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
        test_module = native.package_name().replace("/", ".") + "." + src.removesuffix(".py"),
        test_srcs = [src],
        title = title,
    )
