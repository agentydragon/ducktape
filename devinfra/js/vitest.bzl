"""Macros: vitest_suite, vitest_subsuite — one vitest_test per spec, and a test_suite over them.

Specs reach vitest already compiled: each `*.test.ts[x]` is its own `testonly` `ts_library`
(<ts_library.bzl>), whose tsc action type-checked it and emitted the `.js` that vitest runs.
Nothing is transformed at run time, so no package carries a `vitest.config.*`; `vitest_suite`
generates it from its attributes.

Each spec library gets its own `vitest_test` (`<stem>_vitest`), so a change reruns only the specs
it reaches and a failure names its spec. The `test_suite` `name` collects them, with the suites
of subpackages, so `bbr test //pkg:vitest_test` still means "every spec under this package".

A spec library that nothing runs compiles and lints but never runs, and nothing downstream
notices. So the macros take the spec libraries explicitly and, at load time, check them against
the spec files `glob` finds in the calling package, failing on a file without a listed library
and on a listed library without a file. That makes the library name derivable from the file:
the path relative to the package without `.test.ts[x]`, `/` and `-` written as `_`, plus
`_test` (`actions/rendering/ssh.test.tsx` -> `actions_rendering_ssh_test`).

`glob` stops at package boundaries, so a subpackage's specs are guarded by that subpackage
calling `vitest_subsuite` and the calling package listing the resulting suite in `suites`.
Nothing checks that a subpackage holding specs does either.
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

def _spec_files(owner):
    """The calling package's spec files by the name of the library that must compile each."""
    spec_files = {}
    for spec_file in native.glob(_SPEC_GLOBS, allow_empty = True):
        name = _library_name(spec_file)
        if name in spec_files:
            fail("%s: %s and %s both map to the spec library name `%s`; rename one" % (owner, spec_files[name], spec_file, name))
        spec_files[name] = spec_file
    return spec_files

def _local_name(owner, label):
    package_prefix = "//%s:" % native.package_name()
    if label.startswith(package_prefix):
        return label[len(package_prefix):]
    if label.startswith(":"):
        return label[1:]
    fail("%s: spec library %s is not in //%s; list a subpackage's specs through its `vitest_subsuite`, in `suites`" % (owner, label, native.package_name()))

def _check_specs(owner, spec_files, specs):
    listed = {_local_name(owner, spec): True for spec in specs}
    for name in listed:
        if name not in spec_files:
            fail("%s: lists spec library `%s`, but no spec file in //%s maps to that name; a spec library compiles `<path>.test.ts[x]` and is named `<path>_test`, with `/` and `-` in the path written as `_`" % (owner, name, native.package_name()))
    for name, spec_file in spec_files.items():
        if name not in listed:
            fail("%s: spec file %s has no spec library listed in `specs`; add `ts_library(name = \"%s\", srcs = [\"%s\"], testonly = True, ...)` and list `:%s`" % (owner, spec_file, name, spec_file, name))

def _define_tests(name, specs, suites, config, support, chdir_package, path_prefix, kwargs):
    owner = "//%s:%s" % (native.package_name(), name)
    spec_files = _spec_files(owner)
    _check_specs(owner, spec_files, specs)

    tests = []
    for library, spec_file in sorted(spec_files.items()):
        test = library.removesuffix("_test") + "_vitest"
        vitest_bin.vitest_test(
            name = test,
            args = ["run", "--config", config, path_prefix + spec_file.rsplit(".test.", 1)[0] + ".test.js"],
            chdir = chdir_package,
            data = [support, ":" + library],
            **kwargs
        )
        tests.append(":" + test)
    native.test_suite(name = name, tests = tests + suites)

def vitest_suite(
        name,
        specs,
        environment,
        setup_files = [],
        data = [],
        suites = [],
        **kwargs):
    """One `vitest_test` per spec library of the calling package, and a `test_suite` over them.

    Args:
        name: The `test_suite`: every spec of this package, and of the `suites`.
        specs: This package's spec libraries, each named after its spec file (see the module
            docstring). Checked against the package's spec files.
        environment: vitest's `environment` for every spec without an `// @vitest-environment`
            pragma: `node`, `happy-dom` or `jsdom`. Required: whether a package's specs get a DOM
            is its own decision, and a default would be wrong for most of them. A spec's pragma
            may name another one, whose package must then be in `data`.
        setup_files: Files, relative to the package, vitest runs before each spec file.
        data: Further runtime files, such as the packages pragmas select.
        suites: The `vitest_subsuite` suites of subpackages, which run under this package's
            config.
        **kwargs: Passed to every spec's test, e.g. `size`, `timeout`, `tags`, `env`.
    """
    if environment not in _ENVIRONMENT_PACKAGES:
        fail("%s: environment must be one of %s, got %r" % (name, sorted(_ENVIRONMENT_PACKAGES), environment))

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

    # What every spec's test needs besides its own library; subpackage suites reuse it.
    js_library(
        name = name + "_support",
        testonly = True,
        srcs = [":" + name + "_config"] + setup_files,
        deps = ["//:node_modules/vitest"] + _ENVIRONMENT_PACKAGES[environment] + data,
        visibility = ["//%s:__subpackages__" % native.package_name()],
    )

    _define_tests(name, specs, suites, config, ":" + name + "_support", native.package_name(), "", kwargs)

def vitest_subsuite(name, specs, base, suites = [], **kwargs):
    """Like `vitest_suite`, for a subpackage of the package that defines `base`.

    The specs run from `base`'s package under its config, as they would in its own suite.

    Args:
        name: The `test_suite`: every spec of this package, and of the `suites`.
        specs: This package's spec libraries, as for `vitest_suite`.
        base: The `vitest_suite` of an ancestor package, whose `suites` lists this one.
        suites: The `vitest_subsuite` suites of this package's own subpackages.
        **kwargs: Passed to every spec's test, e.g. `size`, `timeout`, `tags`, `env`.
    """
    base_package, base_name = base.removeprefix("//").split(":")
    if not native.package_name().startswith(base_package + "/"):
        fail("//%s:%s: base %s is not in an ancestor package" % (native.package_name(), name, base))
    _define_tests(
        name,
        specs,
        suites,
        base_name + ".config.mjs",
        "//%s:%s_support" % (base_package, base_name),
        base_package,
        native.package_name().removeprefix(base_package + "/") + "/",
        kwargs,
    )
