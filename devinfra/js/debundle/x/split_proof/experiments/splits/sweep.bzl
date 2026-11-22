"""Register independent tests for the public library split corpus."""

load("@rules_shell//shell:sh_test.bzl", "sh_test")

_SPLIT_PROOF = "//devinfra/js/debundle/x/split_proof"
_TOOLS = [
    _SPLIT_PROOF + "/translation:prepare_real_bundles",
    _SPLIT_PROOF + "/lean:lower",
    ":real_split",
    ":summarize_real_splits",
]

def _real_split_test(bundle, cut_step = 1, entry_first_step = 10):
    name = "real_split_" + bundle.replace(".", "_") + "_test"
    sh_test(
        name = name,
        srcs = ["experiments/splits/run_sweep.sh"],
        args = ["$(rlocationpath %s)" % tool for tool in _TOOLS] + [
            bundle,
            str(cut_step),
            str(entry_first_step),
        ],
        data = _TOOLS,
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
        tests = [
            _real_split_test("acorn"),
            _real_split_test("acorn.min"),
            _real_split_test("lodash"),
            _real_split_test("semver"),
            _real_split_test("zod", cut_step = 4, entry_first_step = 40),
            _real_split_test("zod.min", cut_step = 4, entry_first_step = 40),
        ],
        tags = ["manual"],
    )
