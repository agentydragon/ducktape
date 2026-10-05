"""How a package declares its Playwright visual-render sweep.

One `py_visual_test` per frontend. The package contributes
its harness page and a `scenarios.json`; the sweep (`//util/testing:visual_sweep`) is the test, so
there is no Python in the package. The macro carries what no caller should have to know: the
environment the sweep reads, and the `visual` tag that lets the weekly determinism sweep and the PR
visual job find these targets by query instead of by a hand-maintained roster.

The browser needs no wiring: `//util/testing:frontend_visual` puts the pinned headless shell in the
test's runfiles.
"""

load("//devinfra/python:defs.bzl", "py_test")

def py_visual_test(
        name,
        harness,
        scenarios,
        title,
        assets = [],
        fonts = None,
        font_family = None,
        devtools_viewport = False,
        output_suffix = None,
        inline_page = False,
        stylesheets = [],
        base_href = None,
        page_url = None,
        served_documents = {},
        env = {},
        tags = [],
        **kwargs):
    """A scenario sweep: one fresh browser and one PNG per row of `scenarios`.

    Args:
      name: target name; `visual` by convention, so `//pkg/frontend:visual` names the sweep. A
        `py_test`'s executable is `<package>/<name>`, so a package with a `visual/` directory of
        harness sources cannot use it: the directory's outputs (`visual/dist/harness.js`) would sit
        under the executable's path. Name the directory something else (`harness/`).
      harness: the bundled harness page's JS. The page is the `index.html` beside it, or beside its
        `dist/` directory.
      scenarios: the scenario table, a JSON file (see `//util/testing:visual_scenarios`) shared with
        the harness, or generated from the data the harness already consumes when its rows derive
        from it. The only list of scenarios: BUILD names a shard count, not a scenario, and
        `--test_filter=<scenario>` runs one.
      title: title of the published visual-review manifest.
      assets: everything else the harness page pulls over `file://`: its `index.html`, any
        stylesheet, the bundle rule itself.
      fonts: optional app-owned font filegroup the harness serves alongside its assets.
      font_family: optional named family, which the harness's stylesheet must declare with an
        `@font-face` and which must have loaded. Required with `fonts`, so a custom font asset
        cannot be staged without declaring its purpose.
      output_suffix: what follows a scenario's output name in its PNG's file name; `-actual` if
        unset. A lane migrating from a runner that wrote bare `<name>.png` sets `""`, so its images
        keep their names in PR visual review.
      inline_page: load the harness as a document assembled in memory (`set_content`) from the
        bundle, `stylesheets` and the scenario's `windowGlobals`, not as the `index.html` beside the
        bundle. The page then has no origin: the request fence allows nothing, not even `file://`.
        `harness` may be an esbuild `output_dir`, and `assets` is not needed.
      stylesheets: with `inline_page`, the CSS files inlined into the document, in order.
      base_href: with `inline_page`, the document's `<base href>`, for a harness that parses relative
        URLs its stubbed `fetch` never sends.
      page_url: with `inline_page`, serve the document at this URL instead of loading it with
        `set_content`, so the page has that origin: `localStorage`, `location`, a cross-origin frame. The
        URL is the document's base too, so `base_href` is not also set.
      served_documents: URL prefix to the HTML file the request fence answers a request under it with,
        for a shell that frames another origin (the harness mocks that origin's document). Any other
        request still fails the scenario.
      devtools_viewport: emulate and capture each viewport over the DevTools protocol
        (`DevtoolsViewport`), so a lane keeps its published images byte-identical at a device scale
        factor where Playwright's own viewport differs.
      env: extra environment for the sweep.
      tags: extra tags; `visual` is always added.
      **kwargs: passed to `py_test` -- `size` and `shard_count` in practice.
    """
    if fonts != None and font_family == None:
        fail("py_visual_test(%s) brings its own fonts, so it must name the font_family they force; " % name +
             "left unset, the render assertion cannot verify the app-owned font.")
    if fonts == None and font_family != None:
        fail("py_visual_test(%s) names font_family but does not provide the app-owned fonts; " % name +
             "pass both together.")
    if (stylesheets or base_href != None or page_url != None) and not inline_page:
        fail("py_visual_test(%s) sets stylesheets, base_href or page_url, which only an inline_page uses." % name)
    if page_url != None and base_href != None:
        fail("py_visual_test(%s) sets page_url, which is its own base: drop base_href." % name)

    sweep_env = dict(env)
    sweep_env["HARNESS_PATH"] = "$(rlocationpath %s)" % harness
    sweep_env["SCENARIOS_PATH"] = "$(rlocationpath %s)" % scenarios
    sweep_env["VISUAL_TITLE"] = title
    if font_family:
        sweep_env["EXPECTED_FONT_FAMILY"] = font_family
    if devtools_viewport:
        sweep_env["DEVTOOLS_VIEWPORT"] = "1"
    if output_suffix != None:
        sweep_env["OUTPUT_SUFFIX"] = output_suffix
    if inline_page:
        sweep_env["INLINE_PAGE"] = "1"
        sweep_env["STYLESHEET_PATHS"] = " ".join(["$(rlocationpath %s)" % sheet for sheet in stylesheets])
        if base_href != None:
            sweep_env["BASE_HREF"] = base_href
        if page_url != None:
            sweep_env["PAGE_URL"] = page_url

    if served_documents:
        sweep_env["SERVED_DOCUMENTS"] = json.encode(
            {url: "$(rlocationpath %s)" % document for url, document in served_documents.items()},
        )

    py_test(
        name = name,
        main_module = "util.testing.visual_sweep",
        data = assets + stylesheets + served_documents.values() + [harness, scenarios] +
               ([fonts] if fonts != None else []),
        env = sweep_env,
        tags = tags + ["visual"],
        deps = [
            "//:conftest",
            "//util/testing:visual_sweep",
        ],
        **kwargs
    )
