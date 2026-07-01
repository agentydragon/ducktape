"""XML conduit dependencies for Bazel build."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

def _xml_conduit_deps_impl(_ctx):
    http_archive(
        name = "xml_conduit",
        url = "https://hackage.haskell.org/package/xml-conduit-1.9.1.1/xml-conduit-1.9.1.1.tar.gz",
        sha256 = "bdb117606c0b56ca735564465b14b50f77f84c9e52e31d966ac8d4556d3ff0ff",
        strip_prefix = "xml-conduit-1.9.1.1",
        build_file_content = """
load("@rules_haskell//haskell:cabal.bzl", "haskell_cabal_library")
haskell_cabal_library(
    name = "xml-conduit",
    version = "1.9.1.1",
    srcs = glob(["**"]),
    haddock = False,
    deps = [
        "@stackage//:attoparsec",
        "@stackage//:blaze-html",
        "@stackage//:blaze-markup",
        "@stackage//:conduit",
        "@stackage//:conduit-extra",
        "@stackage//:data-default-class",
        "@stackage//:resourcet",
        "@stackage//:xml-types",
        "@stackage//:cabal-doctest",
    ],
    setup_deps = ["@stackage//:cabal-doctest"],
    visibility = ["//visibility:public"],
)
""",
    )

xml_conduit_deps = module_extension(implementation = _xml_conduit_deps_impl)
