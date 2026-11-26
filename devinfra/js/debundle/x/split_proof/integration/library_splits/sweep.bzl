"""Register independent tests for the public library split corpus."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

_SPLIT_PROOF = "//devinfra/js/debundle/x/split_proof"
_TOOLS = [
    _SPLIT_PROOF + "/lean:lower",
    ":real_split",
    ":summarize_real_splits",
]

_VARIANTS = [
    ("acorn", 1, 10),
    ("acorn.min", 1, 10),
    ("lodash", 1, 10),
    ("semver", 1, 10),
    ("zod", 4, 40),
    ("zod.min", 4, 40),
]

def _real_bundle_artifacts():
    """Generate independently cacheable bundle + AST output pairs."""
    for variant, _, _ in _VARIANTS:
        js_run_binary(
            name = "prepare_" + variant.replace(".", "_"),
            tool = ":prepare",
            args = ["real_bundles", variant],
            chdir = native.package_name(),
            outs = ["real_bundles/" + variant + ext for ext in [".mjs", ".json"]],
        )

def _real_split_test(bundle, cut_step = 1, entry_first_step = 10, smoke = False):
    name = "real_split_" + bundle.replace(".", "_") + "_test"
    bundle_files = [":real_bundles/" + bundle + ext for ext in [".mjs", ".json"]]
    if smoke:
        name = "library_smoke_test"
    sh_test(
        name = name,
        srcs = ["run_sweep.sh"],
        args = ["$(rlocationpath %s)" % tool for tool in _TOOLS + bundle_files] + [
            bundle,
            str(cut_step),
            str(entry_first_step),
        ] + (["--sample"] if smoke else []),
        data = _TOOLS + bundle_files,
        size = "large",
        timeout = "long" if smoke else "eternal",
        tags = [] if smoke else ["manual"],
        use_bash_launcher = True,
    )
    return ":" + name

def real_split_sweeps():
    """Expose one opt-in test per bundle variant and a suite for the full corpus."""
    _real_bundle_artifacts()

    # library_smoke_test is now the direct Node probe in BUILD.bazel. The old
    # semver split shortcut depended on diagnostic output construction.
    native.test_suite(
        name = "real_split_sweep",
        tests = [_real_split_test(bundle, cut, entry_first) for bundle, cut, entry_first in _VARIANTS],
        tags = ["manual"],
    )
