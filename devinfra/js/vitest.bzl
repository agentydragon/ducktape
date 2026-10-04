"""Macros: vitest_suite, vitest_specs — one vitest run over a package's compiled specs.

Specs reach vitest already compiled: each `*.test.ts[x]` is its own `testonly` `ts_library`
(<ts_library.bzl>), whose tsc action type-checked it and emitted the `.js` that vitest runs.
Nothing is transformed at run time, so no package carries a `vitest.config.*`; `vitest_suite`
generates it from its attributes.

A spec library that no suite lists compiles and lints but never runs, and nothing downstream
notices. So the macros take the spec libraries explicitly and, at load time, check them against
the spec files `glob` finds in the calling package, failing on a file without a listed library
and on a listed library without a file. That makes the library name derivable from the file:
the path relative to the package without `.test.ts[x]`, `/` and `-` written as `_`, plus
`_test` (`actions/rendering/ssh.test.tsx` -> `actions_rendering_ssh_test`).

`glob` stops at package boundaries, so a subpackage's specs are guarded by that subpackage
calling `vitest_specs` and the calling package listing the resulting group. Nothing checks
that a subpackage holding specs defines and is listed by one.
"""

load("@aspect_rules_js//js:defs.bzl", "js_library")
load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@npm_ducktape//:vitest/package_json.bzl", vitest_bin = "bin")

_SPEC_GLOBS = ["**/*.test.ts", "**/*.test.tsx"]

# Each environment's implementation package, which vitest imports by name at run time.
_ENVIRONMENT_PACKAGES = {
    "happy-dom": ["//:node_modules/happy-dom"],
    "jsdom": ["//:node_modules/jsdom"],
    "node": [],
}

def _library_name(spec_file):
    return spec_file.rsplit(".test.", 1)[0].replace("/", "_").replace("-", "_") + "_test"

def _local_name(label):
    """The target name of `label` if it is in the calling package, else None."""
    if label.startswith("//") or label.startswith("@"):
        package_prefix = "//%s:" % native.package_name()
        return label[len(package_prefix):] if label.startswith(package_prefix) else None
    return label.removeprefix(":")

def _check_specs(owner, specs):
    spec_files = {}
    for spec_file in native.glob(_SPEC_GLOBS, allow_empty = True):
        name = _library_name(spec_file)
        if name in spec_files:
            fail("%s: %s and %s both map to the spec library name `%s`; rename one" % (owner, spec_files[name], spec_file, name))
        spec_files[name] = spec_file

    listed = {name: True for name in [_local_name(spec) for spec in specs] if name != None}
    for name in listed:
        if name not in spec_files:
            fail("%s: lists spec library `%s`, but no spec file in //%s maps to that name; a spec library compiles `<path>.test.ts[x]` and is named `<path>_test`, with `/` and `-` in the path written as `_`" % (owner, name, native.package_name()))
    for name, spec_file in spec_files.items():
        if name not in listed:
            fail("%s: spec file %s has no spec library listed in `specs`; add `ts_library(name = \"%s\", srcs = [\"%s\"], testonly = True, ...)` and list `:%s`" % (owner, spec_file, name, spec_file, name))

def vitest_specs(name, specs):
    """Groups a subpackage's spec libraries for the suite that runs them.

    Args:
        name: Group target, listed in the `specs` of the suite (or parent group) that runs it.
        specs: This package's spec libraries, plus the groups of its subpackages. The package's
            own libraries must match its spec files exactly (see the module docstring); labels
            in other packages are not checked.
    """
    _check_specs("//%s:%s" % (native.package_name(), name), specs)
    js_library(
        name = name,
        testonly = True,
        deps = specs,
    )

def vitest_suite(
        name,
        specs,
        environment = "happy-dom",
        setup_files = [],
        data = [],
        **kwargs):
    """A `vitest run` over the compiled specs of the calling package and its subpackages.

    Args:
        name: Test target.
        specs: Spec libraries, checked as in `vitest_specs`; labels in other packages are the
            `vitest_specs` groups of subpackages.
        environment: vitest's `environment` for every spec without an `// @vitest-environment`
            pragma: `node`, `happy-dom` or `jsdom`. A spec's pragma may name another one, whose
            package must then be in `data`.
        setup_files: Files, relative to the package, vitest runs before each spec file.
        data: Further runtime files, such as the packages pragmas select.
        **kwargs: Passed to the test, e.g. `size`, `timeout`, `shard_count`, `tags`, `env`.
    """
    if environment not in _ENVIRONMENT_PACKAGES:
        fail("%s: environment must be one of %s, got %r" % (name, sorted(_ENVIRONMENT_PACKAGES), environment))
    _check_specs("//%s:%s" % (native.package_name(), name), specs)

    test = {"environment": environment, "include": ["**/*.test.js"]}
    if setup_files:
        test["setupFiles"] = ["./" + setup_file for setup_file in setup_files]
    config = name + ".config.mjs"
    write_file(
        name = name + "_config",
        out = config,
        content = [
            "// Generated by vitest_suite (//devinfra/js:vitest.bzl).",
            "export default " + json.encode_indent({"cacheDir": ".vitest-cache", "test": test}, indent = "  ") + ";",
        ],
        testonly = True,
        visibility = ["//visibility:private"],
    )

    vitest_bin.vitest_test(
        name = name,
        args = ["run", "--config", config],
        chdir = native.package_name(),
        data = [":" + name + "_config", "//:node_modules/vitest"] + _ENVIRONMENT_PACKAGES[environment] + setup_files + specs + data,
        **kwargs
    )
