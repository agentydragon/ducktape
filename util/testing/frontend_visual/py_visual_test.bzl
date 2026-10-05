"""How a package declares its Playwright visual-render sweep.

One `py_visual_test` per frontend, the Python counterpart of `visual_test`. The package contributes
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
      devtools_viewport: emulate and capture each viewport over the DevTools protocol the way the
        Puppeteer sweep did (`DevtoolsViewport`), so a lane ported from it keeps its images
        byte-identical at a device scale factor where Playwright's own viewport differs.
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

    py_test(
        name = name,
        main_module = "util.testing.visual_sweep",
        data = assets + [harness, scenarios] + ([fonts] if fonts != None else []),
        env = sweep_env,
        tags = tags + ["visual"],
        deps = [
            "//:conftest",
            "//util/testing:visual_sweep",
        ],
        **kwargs
    )
