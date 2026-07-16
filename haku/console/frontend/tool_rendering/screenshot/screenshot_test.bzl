"""Per-server preview screenshot test target.

Each `tool_rendering/<server>/BUILD.bazel` calls `preview_screenshots(name = "previews", ...)`
to produce one `js_test` that renders that server's tool-call preview cards to PNGs (one per
fixture × variant × color scheme) plus a `visual-review.json`. The shared harness — the card
renderer, the mount, the mock, and the Puppeteer driver — lives in
`//haku/console/frontend/tool_rendering/screenshot`; this macro only bundles the server's entry
(an IIFE that imports the shared mount + that server's fixtures) and runs the shared driver.

Co-locating fixtures + target per server means a widget change re-runs only that server's
screenshots (per-target Bazel caching), and `pr_visuals.py` already aggregates every test
target's manifest — so N targets drop straight into the review page.

The native `esbuild()` rule is used (not a hand-rolled driver) because it ships the bazel-sandbox
module resolver that understands rules_js's symlinked `node_modules` layout. IIFE format so the
file:// page can `<script>`-load the bundle without module CORS.
"""

load("@aspect_rules_esbuild//esbuild:defs.bzl", "esbuild")
load("@aspect_rules_js//js:defs.bzl", "js_test")

def preview_screenshots(name, entry, deps, visibility = None):
    """Define a per-server preview screenshot `js_test`.

    Args:
      name: Target name (convention: `previews`).
      entry: The per-server harness entry source (e.g. `preview_harness.tsx`) — imports the
        shared `mountPreviewCards` and passes this server's `PREVIEW_FIXTURES`.
      deps: `js_library` targets forming the entry's module graph — at minimum the entry's own
        library (which Gazelle wires transitively to the shared harness + this server's widgets)
        and `//:node_modules`.
      visibility: Target visibility.
    """
    esbuild(
        name = name + "_bundle",
        entry_point = entry,
        deps = deps,
        config = {"jsx": "automatic"},
        format = "iife",
        platform = "browser",
        target = ["es2022"],
        sourcemap = False,
        minify = False,
        output_dir = True,
        visibility = visibility,
    )
    js_test(
        name = name,
        size = "large",
        entry_point = "//haku/console/frontend/tool_rendering/screenshot:render",
        data = [
            ":%s_bundle" % name,
            "//haku/console/frontend:styles_css",
            "//util/testing/frontend_visual:launcher",
            "//util/testing/frontend_visual:visual_review_manifest",
            "@playwright_browsers//:chromium-headless-shell",
        ],
        env = {
            "HARNESS_JS": "$(rootpath :%s_bundle)" % name,
            "STYLES_CSS": "$(rootpath //haku/console/frontend:styles_css)",
            "CHROMIUM_HEADLESS_SHELL": "$(rootpath @playwright_browsers//:chromium-headless-shell)",
        },
        no_copy_to_bin = [
            ":%s_bundle" % name,
            "//util/testing/frontend_visual:launcher",
            "//util/testing/frontend_visual:visual_review_manifest",
            "@playwright_browsers//:chromium-headless-shell",
        ],
        visibility = visibility,
    )
