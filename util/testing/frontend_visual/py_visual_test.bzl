"""Bazel wiring for package-owned Python visual tests."""

load("//devinfra/python:defs.bzl", "py_test")

def py_visual_test(
        name,
        harness,
        title,
        test_module,
        assets = [],
        fonts = None,
        font_family = None,
        devtools_viewport = False,
        inline_page = False,
        stylesheets = [],
        base_href = None,
        page_url = None,
        served_documents = {},
        test_srcs = [],
        test_deps = [],
        env = {},
        tags = [],
        **kwargs):
    """Run Python tests with hermetic browser assets and visual-review metadata.

    `test_module` is a pytest_bazel entry point. Harness assets are runfiles, not an
    instruction table; the test owns selection, actions, assertions and capture.
    `inline_page` assembles the bundle and stylesheets in memory. `page_url` gives
    that document an origin; `served_documents` supplies mocked iframe documents.
    `devtools_viewport` preserves existing device-pixel viewport captures.
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

    visual_env = dict(env)
    visual_env["HARNESS_PATH"] = "$(rlocationpath %s)" % harness
    visual_env["VISUAL_TITLE"] = title
    if font_family:
        visual_env["EXPECTED_FONT_FAMILY"] = font_family
    if devtools_viewport:
        visual_env["DEVTOOLS_VIEWPORT"] = "1"
    if inline_page:
        visual_env["INLINE_PAGE"] = "1"
        visual_env["STYLESHEET_PATHS"] = " ".join(["$(rlocationpath %s)" % sheet for sheet in stylesheets])
        if base_href != None:
            visual_env["BASE_HREF"] = base_href
        if page_url != None:
            visual_env["PAGE_URL"] = page_url

    if served_documents:
        visual_env["SERVED_DOCUMENTS"] = json.encode(
            {url: "$(rlocationpath %s)" % document for url, document in served_documents.items()},
        )

    py_test(
        name = name,
        main_module = test_module,
        srcs = test_srcs,
        data = assets + stylesheets + served_documents.values() + [harness] +
               ([fonts] if fonts != None else []),
        env = visual_env,
        tags = tags + ["visual"],
        deps = [
            "//:conftest",
            "//util/testing/frontend_visual:fixtures",
        ] + test_deps,
        **kwargs
    )
