"""Generate and compile TypeScript Zod validators from an OpenAPI document."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("//devinfra/js:ts_library.bzl", "ts_library")

def openapi_zod_ts_library(name, schema, config, tsconfig, deps, out = None, schema_files = [], config_files = [], config_deps = [], isolated_typecheck = False, visibility = None):
    """Generate and type-check Zod TypeScript from declared OpenAPI and config files.

    The schema, config, and every relative-local-reference target are action
    inputs. `config_files` declares local modules imported by the config;
    `config_deps` declares runtime package dependencies used by it. `$(location)`
    supplies the primary files' execroot paths without parsing labels. The named
    target is a compiled `ts_library`; the generated source is also available at
    `out`, which defaults to `<name>.ts`.
    """
    if out == None:
        out = name + ".ts"

    schema_label = str(schema)
    config_label = str(config)
    gen_dir = "_{}_hey_api_out".format(name)
    generated_ts = gen_dir + "/zod.gen.ts"
    package_dir = native.package_name()
    bindir_output = "$(BINDIR)/"
    if package_dir:
        bindir_output += package_dir + "/"

    js_run_binary(
        name = "_{}_generate".format(name),
        srcs = [schema, config] + schema_files + config_files + config_deps,
        outs = [generated_ts],
        args = [
            "$(location {})".format(schema_label),
            bindir_output + gen_dir,
            "$(location {})".format(config_label),
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
        deps = deps,
        isolated_typecheck = isolated_typecheck,
        visibility = visibility,
    )
