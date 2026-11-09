load("@rules_rust//rust:defs.bzl", "rust_test")

_ANALYZER = "//devinfra/js/debundle:debundle_analyze"
_PROJECTOR = "//devinfra/js/debundle:debundle_project_analysis"

def debundle_e2e_test(name, deps = [], data = [], env = {}, saved_analysis_tools = False, **kwargs):
    """A `rust_test` over `<name>.rs` with the shared harness and its runtime tools.

    `DEBUNDLE_TV_ORACLE` makes every gate evaluation in the test panic when
    translation validation disagrees with the realizability gate.

    Args:
      name: Test name and source stem.
      deps: Extra crate dependencies beside the shared harness.
      data: Extra runtime files.
      env: Extra environment variables.
      saved_analysis_tools: Also stage the standalone analyzer and projector
        binaries that `debundle_e2e_support::{analyzer_path,projector_path}` read.
      **kwargs: Passed through to `rust_test`.
    """
    if saved_analysis_tools:
        data = data + [_ANALYZER, _PROJECTOR]
        env = dict({
            "DEBUNDLE_TEST_ANALYZER": "$(rlocationpath {})".format(_ANALYZER),
            "DEBUNDLE_TEST_PROJECTOR": "$(rlocationpath {})".format(_PROJECTOR),
        }, **env)
    rust_test(
        name = name,
        srcs = [name + ".rs"],
        crate_root = name + ".rs",
        edition = "2024",
        data = ["//devinfra/js/debundle:debundle_e2e"] + data,
        deps = [":support"] + deps,
        env = dict({
            "DEBUNDLE_TEST_DEBUNDLER": "$(rlocationpath //devinfra/js/debundle:debundle_e2e)",
            "DEBUNDLE_TV_ORACLE": "1",
        }, **env),
        **kwargs
    )
