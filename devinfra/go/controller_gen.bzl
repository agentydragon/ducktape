"""Hermetic controller-gen actions using a rules_go GoPath and Go SDK."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@io_bazel_rules_go//go:def.bzl", "GoPath")
load(":gopath_codegen.bzl", "GO_TOOLCHAIN", "run_gopath_codegen")

def _generate(ctx, output, output_dir, generator, mnemonic, progress_message):
    run_gopath_codegen(
        ctx = ctx,
        gopath = ctx.attr.gopath[GoPath],
        outputs = [output],
        tools = [ctx.attr._controller_gen[DefaultInfo].files_to_run],
        command = """
TOOL="$PWD"/{tool}
OUTDIR="$PWD"/{outdir}
"$TOOL" {generator} {paths} "output:dir=$OUTDIR"
""".format(
            tool = shell.quote(ctx.executable._controller_gen.path),
            outdir = shell.quote(output_dir),
            generator = generator,
            paths = shell.quote("paths=" + ctx.attr.importpath),
        ),
        mnemonic = mnemonic,
        progress_message = progress_message,
    )
    return [DefaultInfo(files = depset([output]))]

def _controller_deepcopy_impl(ctx):
    output = ctx.actions.declare_file("zz_generated.deepcopy.go")
    return _generate(ctx, output, output.dirname, "object", "ControllerGenDeepCopy", "Generating deep-copy methods for %{label}")

def _controller_crds_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name)
    return _generate(ctx, output, output.path, "crd", "ControllerGenCRD", "Generating CRDs for %{label}")

_CONTROLLER_GEN_ATTRS = {
    "gopath": attr.label(mandatory = True, providers = [GoPath]),
    "importpath": attr.string(mandatory = True),
    "_controller_gen": attr.label(
        default = Label("@io_k8s_sigs_controller_tools//cmd/controller-gen"),
        cfg = "exec",
        executable = True,
    ),
}

controller_deepcopy = rule(
    implementation = _controller_deepcopy_impl,
    attrs = _CONTROLLER_GEN_ATTRS,
    toolchains = [GO_TOOLCHAIN],
)

controller_crds = rule(
    implementation = _controller_crds_impl,
    attrs = _CONTROLLER_GEN_ATTRS,
    toolchains = [GO_TOOLCHAIN],
)
