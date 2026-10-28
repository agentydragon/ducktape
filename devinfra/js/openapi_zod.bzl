"""Convert an OpenAPI document file into TypeScript Zod validators."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")

def openapi_zod(name, schema, config, out, schema_files = [], visibility = None):
    """Generate Zod TypeScript from declared OpenAPI and Hey API config files.

    The schema, config, and every relative-local-reference target are action
    inputs. `$(location)` supplies their execroot paths without parsing labels.
    TypeScript compilation belongs to a separate `ts_library` target.
    """
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
        srcs = [schema, config] + schema_files,
        outs = [generated_ts],
        args = [
            "$(location {})".format(schema_label),
            bindir_output + gen_dir,
            "$(location {})".format(config_label),
        ],
        chdir = "../../../",
        tool = "//devinfra/js:openapi_zod_codegen",
    )

    native.genrule(
        name = name,
        srcs = [":" + "_{}_generate".format(name)],
        outs = [out],
        cmd = "cp $< $@",
        visibility = visibility,
    )
