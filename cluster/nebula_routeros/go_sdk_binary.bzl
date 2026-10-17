"""Build a Go binary for another OS/architecture without changing its Bazel configuration."""

load("@bazel_skylib//lib:shell.bzl", "shell")

_GO_TOOLCHAIN = "@io_bazel_rules_go//go:toolchain"

def _go_sdk_binary_impl(ctx):
    srcs = ctx.files.srcs
    if len(srcs) != 1:
        fail("go_sdk_binary currently expects exactly one Go source file")

    sdk = ctx.toolchains[_GO_TOOLCHAIN].sdk
    output = ctx.actions.declare_file(ctx.label.name)
    source = srcs[0]

    ctx.actions.run_shell(
        inputs = depset(
            direct = [source, sdk.root_file, sdk.go, sdk.package_list],
            transitive = [sdk.headers, sdk.libs, sdk.srcs, sdk.tools],
        ),
        tools = [sdk.go],
        outputs = [output],
        command = """
set -euo pipefail
GO="$PWD"/{go}
GOROOT=$("$GO" env GOROOT)
export GOROOT
export PATH="$GOROOT/bin:/usr/bin:/bin"
export CGO_ENABLED=0
export GO111MODULE=off
export GOOS={goos}
export GOARCH={goarch}
export GOARM={goarm}
export GOTOOLCHAIN=local
export GOPROXY=off
export GOSUMDB=off
export GOWORK=off
GOCACHE=$(mktemp -d /tmp/go-sdk-binary-cache.XXXXXX)
trap 'rm -rf "$GOCACHE"' EXIT
export GOCACHE
"$GO" build -trimpath -buildvcs=false -o "$PWD"/{output} "$PWD"/{source}
""".format(
            go = shell.quote(sdk.go.path),
            goarch = shell.quote(ctx.attr.goarch),
            goarm = shell.quote(ctx.attr.goarm),
            goos = shell.quote(ctx.attr.goos),
            output = shell.quote(output.path),
            source = shell.quote(source.path),
        ),
        mnemonic = "GoSDKBinary",
        progress_message = "Cross compiling %{label}",
        use_default_shell_env = False,
    )

    return [DefaultInfo(files = depset([output]))]

go_sdk_binary = rule(
    implementation = _go_sdk_binary_impl,
    attrs = {
        "srcs": attr.label_list(
            allow_files = [".go"],
            mandatory = True,
        ),
        "goos": attr.string(default = "linux"),
        "goarch": attr.string(default = "arm"),
        "goarm": attr.string(default = "7"),
    },
    toolchains = [_GO_TOOLCHAIN],
)
