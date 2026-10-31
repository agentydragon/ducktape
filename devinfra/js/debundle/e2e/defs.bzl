load("@rules_rust//rust:defs.bzl", "rust_test")

def debundle_e2e_test(name, deps = [], env = {}, **kwargs):
    """A `rust_test` over `<name>.rs`; `:support` brings the `debundle` binary and Node into its runfiles.

    `DEBUNDLE_TV_ORACLE` makes every gate evaluation in the test panic when
    translation validation disagrees with the realizability gate.
    """
    rust_test(
        name = name,
        srcs = [name + ".rs"],
        crate_root = name + ".rs",
        edition = "2024",
        deps = [":support"] + deps,
        env = dict({"DEBUNDLE_TV_ORACLE": "1"}, **env),
        **kwargs
    )
