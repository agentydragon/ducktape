"""Rules for building Go binaries using the rules_go SDK directly.

Genrules can't use @io_bazel_rules_go//go:go (blocked as a genrule tool) and
the go_bin_runner wrapper it provides needs runfiles that aren't available in
genrule sandboxes. These rules use ctx.actions.run_shell with the go_bin_runner
in `tools`, which sets up its runfiles properly so `go env GOROOT` works.

Toolchain type: GO_TOOLCHAIN = "@io_bazel_rules_go//go:toolchain"
(defined in go/private/common.bzl, declared as toolchain_type in go/BUILD.bazel)
"""

def _go_sdk_inputs(sdk):
    """All SDK files needed for go build to access the stdlib."""
    return depset(
        [sdk.root_file],
        transitive = [sdk.libs, sdk.headers, sdk.tools, sdk.srcs],
    )

def _plain_binary_impl(ctx):
    sdk = ctx.toolchains["@io_bazel_rules_go//go:toolchain"].sdk
    out = ctx.outputs.out
    go = sdk.go

    ctx.actions.run_shell(
        outputs = [out],
        # sdk.go (go_bin_runner) goes in tools so Bazel sets up its runfiles,
        # letting `go env GOROOT` succeed. SDK files go in inputs for stdlib.
        inputs = depset(ctx.files.srcs, transitive = [_go_sdk_inputs(sdk)]),
        tools = [go],
        command = """
set -euo pipefail
# Capture absolute output path before cd-ing into the temp build dir.
OUT="$PWD/{out}"
GOROOT=$("{go}" env GOROOT)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
cp {srcs} "$T/"
printf 'module garble_target\\ngo 1.26.0\\n' > "$T/go.mod"
export GOROOT GOCACHE="$T/.cache" GOPATH="$T/.gopath"
cd "$T" && "$GOROOT/bin/go" build -o "$OUT" .
""".format(
            go = go.path,
            srcs = " ".join(["'" + f.path + "'" for f in ctx.files.srcs]),
            out = out.path,
        ),
        mnemonic = "GoPlainBinary",
    )

plain_binary = rule(
    implementation = _plain_binary_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True),
        "out": attr.output(mandatory = True),
    },
    toolchains = ["@io_bazel_rules_go//go:toolchain"],
)

def _garble_binary_impl(ctx):
    sdk = ctx.toolchains["@io_bazel_rules_go//go:toolchain"].sdk
    out = ctx.outputs.out
    go = sdk.go
    garble = ctx.executable._garble

    ctx.actions.run_shell(
        outputs = [out],
        inputs = depset(ctx.files.srcs, transitive = [_go_sdk_inputs(sdk)]),
        tools = [go, garble],
        command = """
set -euo pipefail
# Capture absolute paths before cd-ing into the temp build dir.
OUT="$PWD/{out}"
GARBLE="$PWD/{garble}"
GOROOT=$("{go}" env GOROOT)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
cp {srcs} "$T/"
printf 'module garble_target\\ngo 1.26.0\\n' > "$T/go.mod"
export GOROOT
export PATH="$GOROOT/bin:$PATH"
export GOCACHE="$T/.cache" GOPATH="$T/.gopath" XDG_CACHE_HOME="$T/.xdg"
mkdir -p "$T/.xdg"
cd "$T" && "$GARBLE" -seed={seed} build -o "$OUT" .
""".format(
            go = go.path,
            garble = garble.path,
            srcs = " ".join(["'" + f.path + "'" for f in ctx.files.srcs]),
            seed = ctx.attr.seed,
            out = out.path,
        ),
        mnemonic = "GoGarbleBinary",
    )

garble_binary = rule(
    implementation = _garble_binary_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True),
        "seed": attr.string(mandatory = True),
        "out": attr.output(mandatory = True),
        "_garble": attr.label(
            default = "@cc_mvdan_garble//:garble",
            executable = True,
            cfg = "exec",
        ),
    },
    toolchains = ["@io_bazel_rules_go//go:toolchain"],
)
