"""Check entry points of native/default Python test sources during Bazel builds."""

def _pytest_main_impl(target, ctx):
    if ctx.rule.kind != "py_test" or ctx.rule.attr.main or ctx.rule.attr.main_module == "pytest_bazel":
        return []

    # The repo wrapper owns this entry point when it selects pytest_bazel.
    # Match the former pre-commit check for native defaults: conftest.py is pytest
    # support code, not a standalone test entry point. Check direct sources only.
    srcs = [f for f in ctx.rule.files.srcs if f.extension == "py" and f.basename != "conftest.py"]
    if not srcs:
        return []

    report = ctx.actions.declare_file(ctx.label.name + ".pytest_main.ok")
    args = ctx.actions.args()
    args.add(report.path)
    args.add_all(srcs)
    ctx.actions.run(
        executable = ctx.executable._checker,
        inputs = srcs,
        outputs = [report],
        arguments = [args],
        mnemonic = "CheckPytestMain",
        progress_message = "Checking pytest entry points for %{label}",
    )
    return [OutputGroupInfo(pytest_main_checks = depset([report]))]

pytest_main_aspect = aspect(
    implementation = _pytest_main_impl,
    attrs = {
        "_checker": attr.label(
            default = Label("//devinfra/lint:pytest_main_checker"),
            executable = True,
            cfg = "exec",
        ),
    },
)
