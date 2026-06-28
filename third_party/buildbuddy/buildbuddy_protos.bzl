"""Module extension to fetch BuildBuddy proto definitions."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

_COMMIT = "a07198b9707dc26cbce3827d807da6854a978638"
_PREFIX = "buildbuddy-" + _COMMIT

def _buildbuddy_protos_impl(_ctx):
    http_archive(
        name = "buildbuddy_protos",
        url = "https://github.com/buildbuddy-io/buildbuddy/archive/{}.tar.gz".format(_COMMIT),
        integrity = "sha256-QCziTN+n9maFiVJD18q73gcJiwnti5gtaOBtCJIZX1o=",
        strip_prefix = _PREFIX,
        build_file = "//third_party/buildbuddy:BUILD.protos.bazel",
        patch_cmds = [
            # Remove BuildBuddy's own BUILD files so our build_file is the only one.
            "find . -mindepth 2 \\( -name BUILD -o -name BUILD.bazel \\) -delete",
            # Strip vtprotobuf imports and option usages (we don't use vtprotobuf).
            "sed -i -e '/planetscale\\/vtprotobuf/d' -e '/vtproto\\./d' proto/eventlog.proto proto/zip.proto proto/distributed_cache.proto proto/storage.proto",
        ],
    )

buildbuddy_protos = module_extension(
    implementation = _buildbuddy_protos_impl,
)
