"""A `js_test` that drives the pinned browser."""

load("@aspect_rules_js//js:defs.bzl", "js_test")

_CHROMIUM = "@chrome_headless_shell//:executable"

def chromium_js_test(name, data = [], env = {}, no_copy_to_bin = [], **kwargs):
    """A `js_test` that drives the pinned browser.

    Deviation from `js_test`: the browser is added to `data` and left out of bin (its libraries
    and .pak files must stay beside the executable), and its path is `CHROMIUM_HEADLESS_SHELL`.
    """
    js_test(
        name = name,
        data = data + [_CHROMIUM],
        env = env | {"CHROMIUM_HEADLESS_SHELL": "$(rootpath %s)" % _CHROMIUM},
        no_copy_to_bin = no_copy_to_bin + [_CHROMIUM],
        **kwargs
    )
