"""Shared hermetic execution environment for Go source generators."""

load("@bazel_skylib//lib:shell.bzl", "shell")

GO_TOOLCHAIN = "@io_bazel_rules_go//go:toolchain"

def run_gopath_codegen(ctx, gopath, outputs, tools, command, mnemonic, progress_message):
    """Run pinned generators over a declared copy-mode GoPath with the Bazel Go SDK."""
    if not gopath.gopath_file.is_directory:
        fail("gopath must be produced by go_path(mode = \"copy\")")
    sdk = ctx.toolchains[GO_TOOLCHAIN].sdk
    ctx.actions.run_shell(
        inputs = depset(
            direct = [gopath.gopath_file, sdk.root_file, sdk.go, sdk.package_list],
            transitive = [sdk.headers, sdk.libs, sdk.srcs, sdk.tools],
        ),
        tools = [sdk.go] + tools,
        outputs = outputs,
        command = """
set -euo pipefail
GOROOT=$({go} env GOROOT)
export GOROOT
export PATH="$GOROOT/bin:/usr/bin:/bin"
export CGO_ENABLED=0 GO111MODULE=off GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOWORK=off
export GOARCH={goarch} GOOS={goos}
export GOPATH="$PWD"/{gopath}
GOCACHE=$(mktemp -d "${{TMPDIR:-/tmp}}/go-codegen-gocache.XXXXXX")
trap 'rm -rf "$GOCACHE"' EXIT
export GOCACHE
{command}
""".format(
            go = shell.quote(sdk.go.path),
            goarch = shell.quote(sdk.goarch),
            goos = shell.quote(sdk.goos),
            gopath = shell.quote(gopath.gopath_file.path),
            command = command,
        ),
        use_default_shell_env = False,
        mnemonic = mnemonic,
        progress_message = progress_message,
    )
