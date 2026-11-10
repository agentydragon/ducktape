"""Hermetic Crossplane API method and deep-copy generation."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@io_bazel_rules_go//go:def.bzl", "GoPath")
load("//devinfra/go:gopath_codegen.bzl", "GO_TOOLCHAIN", "run_gopath_codegen")

def _provider_codegen_impl(ctx):
    names = [
        "zz_generated.deepcopy.go",
        "zz_generated.managed.go",
        "zz_generated.managedlist.go",
        "zz_generated.pc.go",
        "zz_generated.pcu.go",
        "zz_generated.pculist.go",
    ]
    outputs = [ctx.actions.declare_file(ctx.label.name + "/" + n) for n in names]
    run_gopath_codegen(
        ctx = ctx,
        gopath = ctx.attr.gopath[GoPath],
        outputs = outputs,
        tools = [ctx.attr._controller_gen[DefaultInfo].files_to_run, ctx.attr._angryjet[DefaultInfo].files_to_run],
        command = """
CONTROLLER_GEN="$PWD"/{controller_gen}
ANGRYJET="$PWD"/{angryjet}
OUTDIR="$PWD"/{outdir}
mkdir -p "$GOCACHE/gopath" "$OUTDIR"
cp -R "$GOPATH"/. "$GOCACHE/gopath/"
chmod -R u+w "$GOCACHE/gopath"
export GOPATH="$GOCACHE/gopath"
PKGDIR="$GOPATH/src/"{importpath}
"$CONTROLLER_GEN" object {paths} "output:dir=$PKGDIR"
"$ANGRYJET" generate-methodsets {importpath}
for f in {files}; do cp "$PKGDIR/$f" "$OUTDIR/$f"; done
""".format(
            controller_gen = shell.quote(ctx.executable._controller_gen.path),
            angryjet = shell.quote(ctx.executable._angryjet.path),
            outdir = shell.quote(outputs[0].dirname),
            importpath = shell.quote(ctx.attr.importpath),
            paths = shell.quote("paths=" + ctx.attr.importpath),
            files = " ".join([shell.quote(n) for n in names]),
        ),
        mnemonic = "BuildBuddyAPICodegen",
        progress_message = "Generating Crossplane API methods for %{label}",
    )
    return [DefaultInfo(files = depset(outputs))]

provider_codegen = rule(
    implementation = _provider_codegen_impl,
    attrs = {
        "gopath": attr.label(mandatory = True, providers = [GoPath]),
        "importpath": attr.string(mandatory = True),
        "_controller_gen": attr.label(default = Label("@buildbuddy_controller_tools//cmd/controller-gen"), cfg = "exec", executable = True),
        "_angryjet": attr.label(default = Label("@buildbuddy_crossplane_tools//cmd/angryjet"), cfg = "exec", executable = True),
    },
    toolchains = [GO_TOOLCHAIN],
)
