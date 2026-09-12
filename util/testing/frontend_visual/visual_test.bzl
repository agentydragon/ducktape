"""How a package declares its Puppeteer visual-render test.

One `visual_test` per frontend. What it carries is wiring no caller should have to know: which
files the browser reads where they lie rather than out of bin, the two env vars every runner
reads, and the `visual` tag that lets a sweep find these targets by query instead of by a
hand-maintained roster.
"""

load("@aspect_rules_js//js:defs.bzl", "js_test")

_CHROMIUM = "@playwright_browsers//:chromium-headless-shell"
_LIB = "//util/testing/frontend_visual:visual_test_lib"
_FONTS = "//util/testing/frontend_visual:fonts"

def visual_test(
        name,
        entry_point,
        harness,
        scenarios,
        assets = [],
        fonts = _FONTS,
        font_family = None,
        env = {},
        **kwargs):
    """A scenario sweep over one browser per shard.

    Args:
      name: target name; `visual` by convention, so `//pkg/frontend:visual` names the sweep.
      entry_point: the runner module, which pairs the scenario table with a title and any
        capture overrides. Everything else about running it is this macro's business.
      harness: the bundled harness page's JS, reached through `HARNESS_PATH`.
      scenarios: the scenario table. The one dep copied to bin, because the runner imports it
        rather than the browser fetching it.
      assets: everything else the harness page pulls over `file://` — its `index.html`, any
        stylesheet, the bundle rule itself.
      fonts: the font filegroup the harness serves. Default is the hermetic shared one.
      font_family: the family a page's own typography forces, asserted against what actually
        rendered. Required with `fonts`, since `test-fonts.css`'s Inter is no longer what lands.
      env: extra environment for the runner.
      **kwargs: passed to `js_test` — `size` and `shard_count` in practice.
    """
    if fonts != _FONTS and font_family == None:
        fail("visual_test(%s) brings its own fonts, so it must name the font_family they force; " % name +
             "left unset, the render assertion still checks for the shared test-fonts.css Inter.")

    # Read from the source tree, not copied to bin: the harness page is a file:// URL and pulls
    # these by relative path, so a copy would be a second set of bytes nothing points at.
    read_in_place = assets + [harness, fonts, _CHROMIUM, _LIB]

    runner_env = dict(env)
    runner_env["HARNESS_PATH"] = "$(rootpath %s)" % harness
    runner_env["CHROMIUM_HEADLESS_SHELL"] = "$(rootpath %s)" % _CHROMIUM
    if font_family:
        runner_env["EXPECTED_FONT_FAMILY"] = font_family

    js_test(
        name = name,
        data = read_in_place + [scenarios],
        entry_point = entry_point,
        env = runner_env,
        no_copy_to_bin = read_in_place,
        tags = ["visual"],
        **kwargs
    )
