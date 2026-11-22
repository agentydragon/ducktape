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

def real_bundle_artifacts():
    """Generate independently cacheable bundle + AST output pairs."""
    for variant, _, _ in _VARIANTS:
        js_run_binary(
            name = "prepare_" + variant.replace(".", "_"),
            tool = ":prepare_real_bundles",
            args = ["real_bundles", variant],
            chdir = native.package_name(),
            outs = ["real_bundles/" + variant + ext for ext in [".mjs", ".json"]],
            visibility = [_SPLIT_PROOF + ":__pkg__"],
        )

def _real_split_test(bundle, cut_step = 1, entry_first_step = 10):
    name = "real_split_" + bundle.replace(".", "_") + "_test"
    bundle_files = [_SPLIT_PROOF + "/translation:real_bundles/" + bundle + ext for ext in [".mjs", ".json"]]
    sh_test(
        name = name,
        srcs = ["experiments/splits/run_sweep.sh"],
        args = ["$(rlocationpath %s)" % tool for tool in _TOOLS + bundle_files] + [
            bundle,
            str(cut_step),
            str(entry_first_step),
        ],
        data = _TOOLS + bundle_files,
        size = "large",
        timeout = "eternal",
        tags = ["manual"],
        use_bash_launcher = True,
    )
    return ":" + name

def real_split_sweeps():
    """Expose one opt-in test per bundle variant and a suite for the full corpus."""
    native.test_suite(
        name = "real_split_sweep",
        tests = [_real_split_test(bundle, cut, entry_first) for bundle, cut, entry_first in _VARIANTS],
        tags = ["manual"],
    )
