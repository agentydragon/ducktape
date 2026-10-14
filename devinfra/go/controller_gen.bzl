"""Hermetic controller-gen actions using a rules_go GoPath and Go SDK."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@io_bazel_rules_go//go:def.bzl", "GoPath")

_GO_TOOLCHAIN = "@io_bazel_rules_go//go:toolchain"

def _controller_deepcopy_impl(ctx):
    gopath = ctx.attr.gopath[GoPath]
    if not gopath.gopath_file.is_directory:
        fail("gopath must be produced by go_path(mode = \"copy\")")
    sdk = ctx.toolchains[_GO_TOOLCHAIN].sdk
    output = ctx.actions.declare_file("zz_generated.deepcopy.go")

    ctx.actions.run_shell(
        inputs = depset(
            direct = [gopath.gopath_file, sdk.root_file, sdk.go, sdk.package_list],
            transitive = [sdk.headers, sdk.libs, sdk.srcs, sdk.tools],
        ),
        tools = [sdk.go, ctx.attr._controller_gen[DefaultInfo].files_to_run],
        outputs = [output],
        command = """
set -euo pipefail
GOROOT=$({go} env GOROOT)
export GOROOT
export PATH="$GOROOT/bin:/usr/bin:/bin"
export CGO_ENABLED=0
export GO111MODULE=off
export GOARCH={goarch}
export GOOS={goos}
GOCACHE=$(mktemp -d "${{TMPDIR:-/tmp}}/controller-gen-gocache.XXXXXX")
trap 'rm -rf "$GOCACHE"' EXIT
export GOCACHE
export GOPATH="$PWD"/{gopath}
export GOPROXY=off
export GOSUMDB=off
export GOTOOLCHAIN=local
export GOWORK=off
TOOL="$PWD"/{tool}
OUTDIR="$PWD"/{outdir}
"$TOOL" object {paths} "output:dir=$OUTDIR"
""".format(
            go = shell.quote(sdk.go.path),
            goarch = shell.quote(sdk.goarch),
            goos = shell.quote(sdk.goos),
            gopath = shell.quote(gopath.gopath_file.path),
            tool = shell.quote(ctx.executable._controller_gen.path),
            outdir = shell.quote(output.dirname),
            paths = shell.quote("paths=" + ctx.attr.importpath),
        ),
        use_default_shell_env = False,
        mnemonic = "ControllerGenDeepCopy",
        progress_message = "Generating deep-copy methods for %{label}",
    )

    return [DefaultInfo(files = depset([output]))]

controller_deepcopy = rule(
    implementation = _controller_deepcopy_impl,
    attrs = {
        "gopath": attr.label(mandatory = True, providers = [GoPath]),
        "importpath": attr.string(mandatory = True),
        "_controller_gen": attr.label(
            default = Label("@io_k8s_sigs_controller_tools//cmd/controller-gen"),
            cfg = "exec",
            executable = True,
        ),
    },
    toolchains = [_GO_TOOLCHAIN],
)
