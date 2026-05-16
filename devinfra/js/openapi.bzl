"""Macros that turn an OpenAPI-emitting binary into JS schema artifacts.

Both macros share the same intermediate: ``$(location <generator>) > json``.
They differ in what they run on that JSON:

  - ``js_openapi_schema`` runs ``openapi-typescript`` and produces a ``.d.ts``
    file with type definitions only (no runtime).
  - ``js_openapi_zod`` runs ``openapi-zod-client`` and produces a ``.ts`` file
    with runtime Zod schemas exported as named consts (consumed by esbuild).
"""

load("@aspect_rules_js//js:defs.bzl", "js_library", "js_run_binary")
load("@npm_ducktape//:openapi-typescript/package_json.bzl", openapi_ts_bin = "bin")
load("@npm_ducktape//:openapi-zod-client/package_json.bzl", openapi_zod_bin = "bin")

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

    openapi_ts_bin.openapi_typescript_binary(
        name = "_" + name + "_openapi_ts_bin",
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
        tool = ":_" + name + "_openapi_ts_bin",
    )

    js_library(
        name = name,
        srcs = [":_" + name + "_generate"],
        tags = ["no-lint"],
        visibility = visibility,
    )

def js_openapi_zod(name, generator, out = "api/schema.zod.mjs", visibility = None):
    """Generate a js_library with runtime Zod schemas from an OpenAPI schema.

    Pipeline:

      1. Run *generator* to produce an OpenAPI JSON document.
      2. Rewrite each ``{"const": "X"}`` to ``{"enum": ["X"]}`` so
         ``openapi-zod-client`` emits ``z.enum(["X"])`` (a Zod literal) for
         Pydantic ``Literal`` discriminator fields. Without this it emits
         ``z.string().optional().default("X")``, which Zod 4's
         ``z.discriminatedUnion`` rejects with "Invalid discriminated union
         option at index N" at schema-build time.
      3. Run ``openapi-zod-client --export-schemas`` to emit a ``.ts`` file
         whose ``schemas`` export collects every component schema as a Zod
         object/enum/union.
      4. Strip TypeScript syntax to leave an ``.mjs`` module Node can load
         directly (so JS tests can ``import { schemas }`` without a bundler).

    The generated file also imports from ``@zodios/core`` and emits a Zodios
    endpoints/client we don't consume; esbuild tree-shakes it at bundle time,
    but the dep must be installed so the import statement resolves.

    Args:
        name:       Name of the output ``js_library`` target.
        generator:  Label of the executable that writes OpenAPI JSON to stdout.
        out:        Package-relative path for the generated ``.mjs`` file.
                    Defaults to ``api/schema.zod.mjs``.
        visibility: Visibility of the output ``js_library``.
    """
    raw_json = _openapi_json_genrule(name, generator)
    normalized_json = "_" + name + "_openapi.normalized.json"
    ts_out = out.removesuffix(".mjs") + ".ts" if out.endswith(".mjs") else out + ".ts"

    openapi_zod_bin.openapi_zod_client_binary(
        name = "_" + name + "_openapi_zod_bin",
    )

    js_run_binary(
        name = "_" + name + "_normalize_json",
        srcs = [":_" + name + "_openapi_json"],
        outs = [normalized_json],
        args = [raw_json, normalized_json],
        chdir = native.package_name(),
        tool = "//devinfra/js:openapi_const_to_enum",
    )

    js_run_binary(
        name = "_" + name + "_generate_ts",
        srcs = [":_" + name + "_normalize_json"],
        outs = [ts_out],
        args = [
            normalized_json,
            "-o",
            ts_out,
            "--export-schemas",
        ],
        chdir = native.package_name(),
        tool = ":_" + name + "_openapi_zod_bin",
    )

    js_run_binary(
        name = "_" + name + "_strip_types",
        srcs = [":_" + name + "_generate_ts"],
        outs = [out],
        args = [
            ts_out,
            out,
        ],
        chdir = native.package_name(),
        tool = "//devinfra/js:strip_ts_types",
    )

    js_library(
        name = name,
        srcs = [":_" + name + "_strip_types"],
        tags = ["no-lint"],
        visibility = visibility,
        deps = [
            "//:node_modules/@zodios/core",
            "//:node_modules/zod",
        ],
    )
