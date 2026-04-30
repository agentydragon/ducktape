"""Macro for end-to-end tests that drive run_transform as a black-box binary."""

load("@aspect_rules_js//js:defs.bzl", "js_test")

_RUN_TRANSFORM = "//devinfra/js/debundle/transforms:run_transform"

def e2e_test(name, **kwargs):
    """Declare an e2e test backed by `<name>.mjs`.

    The test imports the shared `:support` js_library and runs the
    run_transform binary, whose path is exported via DUCKTAPE_RUN_TRANSFORM_BIN.
    """
    entry = "{}.mjs".format(name)
    js_test(
        name = name,
        data = [
            entry,
            ":support",
            _RUN_TRANSFORM,
        ],
        entry_point = entry,
        env = {
            "DUCKTAPE_RUN_TRANSFORM_BIN": "$(rootpath {})".format(_RUN_TRANSFORM),
        },
        **kwargs
    )
