"""Generate and compile TypeScript Zod validators from an OpenAPI document."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("//devinfra/js:ts_library.bzl", "ts_library")

def openapi_zod_ts_library(name, schema, tsconfig, out = None, schema_files = [], isolated_typecheck = False, visibility = None):
    """Generate a compiled Zod library. Declare local schema references in schema_files."""
    if out == None:
        out = name + ".ts"

    gen_dir = "_{}_hey_api_out".format(name)
    generated_ts = gen_dir + "/zod.gen.ts"
    package_dir = native.package_name()
    bindir_output = "$(BINDIR)/"
    if package_dir:
        bindir_output += package_dir + "/"

    js_run_binary(
        name = "_{}_generate".format(name),
        srcs = [schema] + schema_files,
        outs = [generated_ts],
        args = [
            "$(location {})".format(schema),
            bindir_output + gen_dir,
        ],
        chdir = "../../../",
        tool = "//devinfra/js:openapi_zod_codegen",
    )

    emit_name = "_{}_emit".format(name)
    native.genrule(
        name = emit_name,
        srcs = [":" + "_{}_generate".format(name)],
        outs = [out],
        # Copy the generated file's contents rather than symlinking it: esbuild
        # resolves imported modules to real paths, and the generator directory is
        # not an input of downstream bundle actions.
        cmd = "cp $< $@",
        visibility = visibility,
    )

    ts_library(
        name = name,
        srcs = [":" + emit_name],
        tsconfig = tsconfig,
        deps = ["//:node_modules/zod"],
        isolated_typecheck = isolated_typecheck,
        visibility = visibility,
    )
