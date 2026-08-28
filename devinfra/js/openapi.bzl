"""Macros that turn an OpenAPI-emitting binary into JS schema artifacts.

Both macros share the same intermediate: ``$(location <generator>) > json``.
They differ in what they run on that JSON:

  - ``js_openapi_schema`` runs ``openapi-typescript`` and produces a ``.d.ts``
    file with type definitions only (no runtime).
  - ``js_openapi_zod`` runs ``@hey-api/openapi-ts`` with the Zod plugin and
    produces a ``.mjs`` module of native Zod 4 schemas exported as
    ``z<Name>`` consts (consumed by esbuild or Node directly).
"""

load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@npm_ducktape//:@hey-api/openapi-ts/package_json.bzl", openapi_ts_bin = "bin")
load("@npm_ducktape//:openapi-typescript/package_json.bzl", openapi_typescript_bin = "bin")
load("//devinfra/js:ts_library.bzl", "ts_library")

def _openapi_json_genrule(name, generator):
    json_out = "_" + name + "_openapi.json"
    native.genrule(
        name = "_" + name + "_openapi_json",
        outs = [json_out],
        cmd = "$(location {}) > $@".format(generator),
        tools = [generator],
    )
    return json_out

def js_openapi_schema(name, generator, out = "api/schema.d.ts", visibility = None):
    """Generate a js_library with TypeScript type definitions from an OpenAPI schema.

    Runs *generator* (a binary that prints an OpenAPI JSON schema to stdout),
    then pipes the result through ``openapi-typescript`` to produce a ``.d.ts``
    file, and wraps that in a ``js_library`` target.

    Args:
        name:       Name of the output ``js_library`` target.
        generator:  Label of the executable that writes OpenAPI JSON to stdout.
        out:        Package-relative path for the generated ``.d.ts`` file.
                    Defaults to ``api/schema.d.ts``.
        visibility: Visibility of the output ``js_library``.
    """
    json_out = _openapi_json_genrule(name, generator)

    openapi_typescript_bin.openapi_typescript_binary(
        name = "_" + name + "_openapi_typescript_bin",
    )

    js_run_binary(
        name = "_" + name + "_generate",
        srcs = [":_" + name + "_openapi_json"],
        outs = [out],
        args = [
            json_out,
            "-o",
            out,
        ],
        chdir = native.package_name(),
        tool = ":_" + name + "_openapi_typescript_bin",
    )

    js_library(
        name = name,
        srcs = [":_" + name + "_generate"],
        tags = ["no-lint"],
        visibility = visibility,
    )

def js_openapi_zod(name, generator, out = "api/schema.zod.mjs", tsconfig = None, isolated_typecheck = False, visibility = None):
    """Generate a js_library with runtime Zod 4 schemas from an OpenAPI schema.

    Pipeline:

      1. Run *generator* to produce an OpenAPI JSON document.
      2. ``@hey-api/openapi-ts --plugins zod`` emits ``<out_dir>/zod.gen.ts``
         with one ``export const z<Name>`` per OpenAPI component schema.
      3. Emit *out*: a ``.ts``/``.mts`` *out* keeps the generated TypeScript
         verbatim (so TypeScript consumers retain Zod's inferred types end to
         end; the file goes through their bundler/type-checker anyway), while a
         ``.mjs`` *out* strips the TypeScript syntax so Node/pure-JS consumers
         can import it without a bundler.

    Hey-api's Zod 4 plugin handles ``"const"`` discriminators, two-arg
    ``z.record``, and ``.extend({ <key>: z.literal(...) })`` overrides for
    ``z.discriminatedUnion`` directly — no post-codegen rewrites needed.

    Args:
        name:       Name of the output ``js_library`` target.
        generator:  Label of the executable that writes OpenAPI JSON to stdout.
        out:        Package-relative path for the generated schema module. A
                    ``.ts``/``.mts`` suffix keeps TypeScript; any other suffix
                    (default ``api/schema.zod.mjs``) strips types to JavaScript.
        tsconfig:   Label of the ``ts_config`` the generated module compiles
                    against. Required for a TypeScript *out* and rejected for a
                    JavaScript one, since only the former is compiled.
        visibility: Visibility of the output ``js_library``.
    """
    emits_typescript = out.endswith(".ts") or out.endswith(".mts")
    if emits_typescript and not tsconfig:
        fail("js_openapi_zod: a TypeScript out (%s) is compiled, so it needs a tsconfig" % out)
    if tsconfig and not emits_typescript:
        fail("js_openapi_zod: out %s is JavaScript; nothing compiles it, so tsconfig is unused" % out)

    json_out = _openapi_json_genrule(name, generator)
    gen_dir = "_" + name + "_hey_api_out"
    gen_ts = gen_dir + "/zod.gen.ts"

    openapi_ts_bin.openapi_ts_binary(
        name = "_" + name + "_openapi_ts_bin",
    )

    js_run_binary(
        name = "_" + name + "_generate_ts",
        srcs = [":_" + name + "_openapi_json"],
        outs = [gen_ts],
        args = [
            "--input",
            json_out,
            "--output",
            gen_dir,
            "--plugins",
            "zod",
        ],
        chdir = native.package_name(),
        tool = ":_" + name + "_openapi_ts_bin",
    )

    if emits_typescript:
        # A real content copy, not `copy_file`'s symlink: esbuild resolves imported modules to
        # their realpath, and the gen-dir `zod.gen.ts` the symlink points at is not an input of
        # downstream bundle actions, so a symlinked `out` makes esbuild fail with
        # "Could not read from file .../zod.gen.ts".
        native.genrule(
            name = "_" + name + "_emit",
            srcs = [":_" + name + "_generate_ts"],
            outs = [out],
            cmd = "cp $< $@",
        )
        emit = ":_" + name + "_emit"
    else:
        js_run_binary(
            name = "_" + name + "_strip_types",
            srcs = [":_" + name + "_generate_ts"],
            outs = [out],
            args = [gen_ts, out],
            chdir = native.package_name(),
            tool = "//devinfra/js:strip_ts_types",
        )
        emit = ":_" + name + "_strip_types"

    # A TypeScript out is a module like any other and compiles through ts_library, so its
    # consumers resolve it by declaration. A stripped JavaScript out has nothing to compile.
    if emits_typescript:
        ts_library(
            name = name,
            srcs = [emit],
            isolated_typecheck = isolated_typecheck,
            tsconfig = tsconfig,
            tags = ["no-lint"],
            visibility = visibility,
            deps = ["//:node_modules/zod"],
        )
    else:
        js_library(
            name = name,
            srcs = [emit],
            tags = ["no-lint"],
            visibility = visibility,
            deps = ["//:node_modules/zod"],
        )
