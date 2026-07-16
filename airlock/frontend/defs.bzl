"""Shared Bazel macros for airlock/frontend visual render-health tests."""

load("@aspect_rules_js//js:defs.bzl", "js_test")

_VISUAL_BASE_DATA = [
    "//util/testing/frontend_visual:visual_test_lib",
    "//airlock/frontend:harness_bundle",
    "//airlock/frontend:harness_js",
    "//airlock/frontend:visual_test_assets",
    "//util/testing/frontend_visual:fonts",
    "//util/testing/frontend_visual:fonts_conf",
    "@playwright_browsers//:chromium-headless-shell",
]

_VISUAL_ENV = {
    "HARNESS_PATH": "$(rootpath //airlock/frontend:harness_js)",
    "PUPPETEER_EXECUTABLE_PATH": "$(rootpath @playwright_browsers//:chromium-headless-shell)",
    "FONTCONFIG_FILE": "$(rootpath //util/testing/frontend_visual:fonts_conf)",
    "FREETYPE_PROPERTIES": "cff:no-stem-darkening=1",
}

def visual_test(name, srcs, deps = [], size = "small"):
    """Render-health + PR-visuals publication test for a single harness scenario.

    Args:
        name: Bazel target name (e.g., "visual_ListPage").
        srcs: Test source files (entry point first).
        deps: Additional data deps this test requires.
        size: Bazel test size (default "small").
    """
    js_test(
        name = name,
        size = size,
        entry_point = srcs[0],
        data = _VISUAL_BASE_DATA + deps,
        env = _VISUAL_ENV,
        no_copy_to_bin = _VISUAL_BASE_DATA,
    )

_VIEWPORTS = {
    "desktop": {"w": "1200", "h": "800"},
    "mobile": {"w": "375", "h": "812"},
}

_THEMES = ["light", "dark"]

def visual_test_matrix(pages, viewports = ["desktop", "mobile"], themes = _THEMES, deps = []):
    """Generate visual regression tests for all page x viewport x theme combinations.

    Args:
        pages: List of harness page names (e.g., ["ListPage", "DetailPage"]).
        viewports: List of viewport names from _VIEWPORTS (default: desktop + mobile).
        themes: List of color schemes (default: light + dark).
        deps: Additional data deps shared by all tests.
    """
    runner = "tests/visual_test_runner.mjs"

    for page in pages:
        for viewport in viewports:
            vp = _VIEWPORTS[viewport]
            for theme in themes:
                suffix_parts = []
                if viewport != "desktop":
                    suffix_parts.append(viewport)
                if theme != "light":
                    suffix_parts.append(theme)

                suffix = ("_" + "_".join(suffix_parts)) if suffix_parts else ""
                output_name = page + suffix
                target_name = "visual_" + output_name

                env = dict(_VISUAL_ENV)
                env["VISUAL_TEST_PAGE"] = page
                env["VISUAL_TEST_OUTPUT_NAME"] = output_name
                env["VISUAL_TEST_COLOR_SCHEME"] = theme
                env["VISUAL_TEST_VIEWPORT_W"] = vp["w"]
                env["VISUAL_TEST_VIEWPORT_H"] = vp["h"]

                js_test(
                    name = target_name,
                    size = "small",
                    entry_point = runner,
                    data = _VISUAL_BASE_DATA + deps,
                    env = env,
                    no_copy_to_bin = _VISUAL_BASE_DATA,
                )
