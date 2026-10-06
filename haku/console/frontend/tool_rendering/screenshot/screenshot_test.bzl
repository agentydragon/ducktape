"""Per-server preview screenshot test target.

Each `tool_rendering/<server>/BUILD.bazel` calls `preview_screenshots(name = "previews", ...)`
to produce one `py_visual_test` that renders that server's tool-call preview cards to PNGs (one per
fixture × variant × color scheme) plus a `visual-review.json`. The shared harness — the card
renderer, the mount, the mock — lives in `//haku/console/frontend/tool_rendering/screenshot`; this
macro only bundles the server's entry (an IIFE that imports the shared mount + that server's
fixtures), derives the scenario table from the server's fixtures, and runs the shared sweep
(`//util/testing:visual_sweep`).

Co-locating fixtures + target per server means a widget change re-runs only that server's
screenshots (per-target Bazel caching), and `pr_visuals.py` already aggregates every test
target's manifest — so N targets drop straight into the review page.

The native `esbuild()` rule is used (not a hand-rolled driver) because it ships the bazel-sandbox
module resolver that understands rules_js's symlinked `node_modules` layout. IIFE format so the
page can inline the bundle in a `<script>` without module CORS.
"""

load("@aspect_rules_esbuild//esbuild:defs.bzl", "esbuild")
load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("//util/testing/frontend_visual:py_visual_test.bzl", "py_visual_test")

def preview_screenshots(name, entry, fixtures, deps, visibility = None):
    """Define a per-server preview screenshot `py_visual_test`.

    Args:
      name: Target name (convention: `previews`).
      entry: The compiled per-server harness entry (e.g. `preview_harness.js`, what the
        `ts_library` wrapping `preview_harness.tsx` emits) — imports the shared
        `mountPreviewCards` and mounts this server's `PREVIEW_FIXTURES`.
      fixtures: The compiled module (e.g. `preview_fixtures.js`) exporting that
        `PREVIEW_FIXTURES`, in this package. The scenario table is generated from it, so the
        fixtures are the only list of what is rendered.
      deps: Library targets forming the entry's module graph — at minimum the entry's own
        library (which Gazelle wires transitively to the shared harness + this server's widgets).
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
    js_run_binary(
        name = name + "_scenarios",
        srcs = [fixtures],
        outs = [name + "_scenarios.json"],
        args = [fixtures, name + "_scenarios.json"],
        chdir = native.package_name(),
        tool = "//haku/console/frontend/tool_rendering/screenshot:emit_scenarios",
    )
    py_visual_test(
        name = name,
        # medium: the largest of these (grocy, 36 shots each on its own browser) measures 79s alone and up
        # to 130s with all seven running at once. large gave a 900s budget to under two minutes of work.
        size = "medium",
        harness = ":%s_bundle" % name,
        scenarios = ":%s_scenarios" % name,
        title = "Haku Console previews",
        # The harness is inlined into a page of no origin, whose relative `/api/…` URLs the mock
        # fetch parses and never sends: the base gives them something to resolve against.
        inline_page = True,
        stylesheets = ["//haku/console/frontend:styles_css"],
        base_href = "https://haku-console.test/",
        output_suffix = "",
        visibility = visibility,
    )
