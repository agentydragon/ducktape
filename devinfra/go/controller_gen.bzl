"""Hermetic controller-gen actions using a rules_go GoPath and Go SDK."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@io_bazel_rules_go//go:def.bzl", "GoPath")
load(":gopath_codegen.bzl", "GO_TOOLCHAIN", "run_gopath_codegen")

def _controller_deepcopy_impl(ctx):
    gopath = ctx.attr.gopath[GoPath]
    output = ctx.actions.declare_file("zz_generated.deepcopy.go")
    run_gopath_codegen(
        ctx = ctx,
        gopath = gopath,
        outputs = [output],
        tools = [ctx.attr._controller_gen[DefaultInfo].files_to_run],
        command = """
TOOL="$PWD"/{tool}
OUTDIR="$PWD"/{outdir}
"$TOOL" object {paths} "output:dir=$OUTDIR"
""".format(
            tool = shell.quote(ctx.executable._controller_gen.path),
            outdir = shell.quote(output.dirname),
            paths = shell.quote("paths=" + ctx.attr.importpath),
        ),
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
    toolchains = [GO_TOOLCHAIN],
)

def _controller_crds_impl(ctx):
    gopath = ctx.attr.gopath[GoPath]
    output = ctx.actions.declare_directory(ctx.label.name)
    run_gopath_codegen(
        ctx = ctx,
        gopath = gopath,
        outputs = [output],
        tools = [ctx.attr._controller_gen[DefaultInfo].files_to_run],
        command = """
TOOL="$PWD"/{tool}
OUTDIR="$PWD"/{outdir}
"$TOOL" crd {paths} "output:dir=$OUTDIR"
""".format(
            tool = shell.quote(ctx.executable._controller_gen.path),
            outdir = shell.quote(output.path),
            paths = shell.quote("paths=" + ctx.attr.importpath),
        ),
        mnemonic = "ControllerGenCRD",
        progress_message = "Generating CRDs for %{label}",
    )
    return [DefaultInfo(files = depset([output]))]

controller_crds = rule(
    implementation = _controller_crds_impl,
    attrs = {
        "gopath": attr.label(mandatory = True, providers = [GoPath]),
        "importpath": attr.string(mandatory = True),
        "_controller_gen": attr.label(
            default = Label("@io_k8s_sigs_controller_tools//cmd/controller-gen"),
            cfg = "exec",
            executable = True,
        ),
    },
    toolchains = [GO_TOOLCHAIN],
)
