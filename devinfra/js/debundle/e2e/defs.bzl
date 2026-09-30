load("@rules_rust//rust:defs.bzl", "rust_test")

def debundle_e2e_test(name, deps = [], **kwargs):
    """A `rust_test` over `<name>.rs`; `:support` brings the `debundle` binary and Node into its runfiles."""
    rust_test(
        name = name,
        srcs = [name + ".rs"],
        crate_root = name + ".rs",
        edition = "2024",
        deps = [":support"] + deps,
        **kwargs
    )
