load("@rules_haskell//haskell:defs.bzl", "haskell_binary")

# Note: Haskell toolchain requires libtinfo.so.5 which may not be available in all environments
haskell_binary(
    name = "site",
    srcs = ["site.hs"],
    tags = ["manual"],
    deps = [
        "@stackage//:base",
        "@stackage//:hakyll",
    ],
)
