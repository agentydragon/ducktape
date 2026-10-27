"""Whole-program, type-aware ESLint tests for TypeScript frontends."""

load("@npm_ducktape//:eslint/package_json.bzl", eslint_bin = "bin")

_TYPED_ESLINT_DATA = [
    "//:node_modules/@types/react",
    "//:node_modules/@typescript-eslint/eslint-plugin",
    "//:node_modules/@typescript-eslint/parser",
    "//:node_modules/eslint-plugin-react",
    "//:node_modules/eslint-plugin-react-hooks",
    "//:node_modules/react",
    "//:node_modules/react-dom",
    "//:node_modules/vitest",
]

def typed_eslint_test(name, srcs, tsconfig, extra_data = [], source_data = [], config = None):
    """Run type-aware ESLint over a whole frontend TypeScript program.

    Args:
        name: Test target name.
        srcs: Filegroup containing every authored TypeScript source in the program.
        tsconfig: The program's checked-in tsconfig.json file.
        extra_data: Generated declarations, additional tsconfigs, and npm packages
            required to resolve the program's imports.
        source_data: Authored TypeScript source groups outside the package that are
            imported directly by this program and should remain at their source paths.
        config: Optional config file relative to the package, for project-specific
            parser options. The shared typed config is used by default.
    """
    package = native.package_name()
    if config:
        config_path = config
        config_data = [config]
    else:
        config_path = "/".join([".."] * len(package.split("/")) + ["devinfra", "js", "eslint.typed.config.js"])
        config_data = []

    eslint_bin.eslint_test(
        name = name,
        args = [
            "--no-config-lookup",
            "--config",
            config_path,
            ".",
        ],
        chdir = package,
        data = [
            "//devinfra/js:eslint_typed_config",
            tsconfig,
            srcs,
        ] + config_data + _TYPED_ESLINT_DATA + extra_data + source_data,
        # The per-module ts_library actions already copy these files to bin. ESLint
        # reads the whole-program source group in place so two actions do not write
        # the same output paths.
        no_copy_to_bin = [srcs] + source_data,
        tags = ["lint"],
    )
