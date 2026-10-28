"""Opt-in proof of type-aware lint on each ts_library's own compiler program."""

load("@aspect_rules_lint//lint:eslint.bzl", "lint_eslint_aspect")

eslint = lint_eslint_aspect(
    binary = Label("//devinfra/lint/x/typed_eslint:eslint"),
    configs = [Label("//devinfra/lint/x/typed_eslint:config")],
    use_tsconfig = True,
)
