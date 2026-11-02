load("@rules_rust//rust:defs.bzl", "rust_test")

def debundle_e2e_test(name, deps = [], data = [], env = {}, **kwargs):
    """A `rust_test` over `<name>.rs` with the shared harness and its runtime tools.

    `DEBUNDLE_TV_ORACLE` makes every gate evaluation in the test panic when
    translation validation disagrees with the realizability gate.
    """
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
