"Linter aspects and test rules for the repository."

load("@aspect_rules_lint//lint:eslint.bzl", "lint_eslint_aspect")
load("@aspect_rules_lint//lint:lint_test.bzl", "lint_test")
load("@aspect_rules_lint//lint:ruff.bzl", "lint_ruff_aspect")
load("@pip_types//:types.bzl", "types")
load("@rules_mypy//mypy:mypy.bzl", "mypy")

# Ruff aspect for --config=lint builds
# Uses ruff from the multitool lockfile bundled with aspect_rules_lint
ruff = lint_ruff_aspect(
    binary = "@multitool//tools/ruff",
    configs = [
        Label("//:ruff.toml"),
    ],
)

# Mypy aspect for --config=typecheck builds
# Uses root mypy.ini for configuration
# Uses custom mypy_cli to run under Python 3.13 (needed to parse homeassistant's 3.13 syntax)
#
# Type checking behavior:
# - Packages with py.typed (rich, structlog, aiohttp, aiodocker) are fully
#   type-checked - API misuse will be caught
# - Packages without py.typed (colorama, Pygments) need type stubs for full
#   checking. Pre-commit has these but Bazel needs a separate pip hub.
#   For now these packages get ignore_missing_imports treatment.
mypy_aspect = mypy(
    mypy_cli = Label("//tools/lint:mypy_cli"),
    mypy_ini = Label("//:mypy.ini"),
    types = types,
    # Disable cache propagation to prevent CI disk exhaustion.
    # Without this, each target writes new cache files for pypi deps it uses,
    # leading to O(n²) disk usage with ~9,670 targets.
    # TODO: Enable include_external=True once site-packages issue is fixed in fork
    cache = False,
)

# ESLint aspect for JS/TS linting
# Uses workspace-level eslint.config.js with per-project file patterns
# ESLint binary and all plugins provided by workspace npm packages
eslint = lint_eslint_aspect(
    binary = Label("//tools/lint:eslint"),
    configs = [
        Label("//:eslintrc"),
    ],
)

# NOTE: Prettier is a formatter, not a linter - handled via //tools/format:format
# Run formatting with: bazel run //tools/format

# Test rule factories - use these in BUILD.bazel files:
#   load("//tools/lint:linters.bzl", "ruff_test")
#   ruff_test(name = "ruff", srcs = [":my_library"])
ruff_test = lint_test(aspect = ruff)
eslint_test = lint_test(aspect = eslint)

# NOTE: mypy_aspect is used via --config=typecheck, not via lint_test
# The rules_mypy aspect produces different output groups than lint_test expects

# NOTE: Clippy uses rules_rust native aspects, not aspect_rules_lint
# Run via: bazel build --config=rust-check //finance/...
