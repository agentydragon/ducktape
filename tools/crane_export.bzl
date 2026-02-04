"""Module extension to re-export the crane binary with public visibility.

rules_oci's generated crane repo has package-private visibility, so py_test
targets can't reference it as a data dep. This extension creates a thin
wrapper repo that symlinks the crane binary and exports it publicly.
"""

def _crane_repo_impl(ctx):
    ctx.symlink(ctx.path(Label("@oci_crane_linux_amd64//:crane")), "crane")
    ctx.file("BUILD.bazel", 'exports_files(["crane"], visibility = ["//visibility:public"])\n')

_crane_repo = repository_rule(implementation = _crane_repo_impl)

def _crane_export_impl(_module_ctx):
    _crane_repo(name = "crane")

crane_export = module_extension(implementation = _crane_export_impl)
