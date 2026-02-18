"""Shared Bazel macros for props/frontend sub-packages."""

load("@aspect_rules_js//js:defs.bzl", "js_test")

# Assets shared by every visual scenario test.
_VISUAL_BASE_DATA = [
    "//props/frontend:visual_test_lib",
    "//props/frontend:harness_bundle",
    "//props/frontend:harness_js",
    "//props/frontend:visual_test_assets",
    "//props/frontend:fonts_conf",
    "@playwright_browsers//:chromium-headless-shell",
]

_VISUAL_ENV = {
    "HARNESS_PATH": "$(rootpath //props/frontend:harness_js)",
    "PUPPETEER_EXECUTABLE_PATH": "$(rootpath @playwright_browsers//:chromium-headless-shell)",
    "FONTCONFIG_FILE": "$(rootpath //props/frontend:fonts_conf)",
    "FREETYPE_PROPERTIES": "cff:no-stem-darkening=1",
}

def visual_test(name, srcs, deps = [], baseline = None, size = "small"):
    """Visual regression test for a single harness scenario.

    Args:
        name: Bazel target name (e.g., "visual_CoverageHeatmap").
        srcs: Test source files (entry point first).
        deps: Component targets this test depends on.
        baseline: Baseline PNG path (e.g., "baselines/Foo-chromium-linux.png").
        size: Bazel test size (default "small").
    """
    js_test(
        name = name,
        size = size,
        entry_point = srcs[0],
        data = _VISUAL_BASE_DATA + deps + ([baseline] if baseline else []),
        env = _VISUAL_ENV,
        no_copy_to_bin = _VISUAL_BASE_DATA,
    )
