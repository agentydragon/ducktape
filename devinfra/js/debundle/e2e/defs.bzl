load("@rules_rust//rust:defs.bzl", "rust_test")

def debundle_e2e_test(name, deps = [], node = True, **kwargs):
    """A `rust_test` over `<name>.rs` that drives the `debundle` binary through `:support`.

    `node = False` omits the Node runtime, for tests that never execute emitted JS.
    """
    rust_test(
        name = name,
        srcs = [name + ".rs"],
        crate_root = name + ".rs",
        data = ["//devinfra/js/debundle"] + (["@nodejs_linux_amd64//:bin/node"] if node else []),
        edition = "2024",
        deps = [":support"] + deps,
        **kwargs
    )
