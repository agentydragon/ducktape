"""Macro for running graph-backed Debundle CLI commands from Bazel."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

_DEBUNDLER = Label("@ducktape//devinfra/js/debundle:debundler")
_LAUNCHER = Label("//devinfra/js/debundle:debundle_cli.sh")

def _debundler_tool_impl(ctx):
    debundler = ctx.executable.debundler
    files = [debundler]
    return [DefaultInfo(
        files = depset(files),
        runfiles = ctx.runfiles(files = files).merge(ctx.attr.debundler[DefaultInfo].default_runfiles),
    )]

_debundler_tool = rule(
    implementation = _debundler_tool_impl,
    attrs = {
        "debundler": attr.label(
            mandatory = True,
            executable = True,
            cfg = "exec",
        ),
    },
)

def _single_tree_impl(ctx):
    files = ctx.attr.src[DefaultInfo].files.to_list()
    if len(files) != 1 or not files[0].is_directory:
        fail("{} must provide exactly one directory artifact".format(ctx.attr.src.label))

    return [DefaultInfo(
        files = depset(files),
        runfiles = ctx.runfiles(files = files).merge(ctx.attr.src[DefaultInfo].default_runfiles),
    )]

_single_tree = rule(
    implementation = _single_tree_impl,
    attrs = {
        "src": attr.label(
            mandatory = True,
            allow_files = True,
        ),
    },
)

def _checked_tree(name, src, testonly = None):
    target = name + "__tree"
    kwargs = {
        "name": target,
        "src": src,
        "visibility": ["//visibility:private"],
    }
    if testonly != None:
        kwargs["testonly"] = testonly
    _single_tree(
        **kwargs
    )
    return ":" + target

def debundle_cli(
        name,
        debundle,
        graph_relpath,
        modules_path,
        source_root = None,
        source_data = None,
        spec_root = None,
        graph_source_data = None,
        package_roots = {},
        debundler = _DEBUNDLER,
        visibility = None,
        tags = None,
        testonly = None):
    """Creates a Bazel runner for commands that consume a saved dependency graph.

    `source_root` is workspace-relative unless `graph_source_data` is set, in
    which case it is relative to that generated tree. `source_data` is an
    alternative generated source tree.

    Args:
      name: Runner target name; internal runfiles targets use this as a prefix.
      debundle: Pipeline target that exposes the `dependency_graphs` output group.
      graph_relpath: Relative path to the selected dependency graph in that tree.
      modules_path: Workspace-relative directory exported as `DEBUNDLE_MODULES`.
      source_root: Workspace-relative selector root, or path under `graph_source_data`.
      source_data: Generated directory artifact used as the selector root.
      spec_root: Workspace-relative spec root passed to graph-backed commands.
      graph_source_data: Relocatable generated directory used for graph source inputs.
      package_roots: Directory targets mapped to saved-graph package names.
      debundler: CLI executable label; defaults to the pipeline's public label flag.
      visibility: Optional visibility for the runner target.
      tags: Optional Bazel tags for the runner target.
      testonly: Optional test-only setting shared by the runner and its helpers.
    """
    if source_data and source_root:
        fail("set either source_data or source_root, not both")
    if not source_data and not source_root:
        fail("set source_data or source_root")
    if not graph_relpath:
        fail("graph_relpath must be nonempty")
    if not modules_path:
        fail("modules_path must be nonempty")

    graph_output = name + "__graph_output"
    graph_kwargs = {
        "name": graph_output,
        "srcs": [debundle],
        "output_group": "dependency_graphs",
        "visibility": ["//visibility:private"],
    }
    if testonly != None:
        graph_kwargs["testonly"] = testonly
    native.filegroup(**graph_kwargs)
    graph_tree = _checked_tree(name + "__graph", ":" + graph_output, testonly)
    debundler_tool = name + "__debundler"
    debundler_kwargs = {
        "name": debundler_tool,
        "debundler": debundler,
        "visibility": ["//visibility:private"],
    }
    if testonly != None:
        debundler_kwargs["testonly"] = testonly
    _debundler_tool(**debundler_kwargs)
    debundler_tool = ":" + debundler_tool

    args = [
        "--debundler",
        "$(rlocationpath {})".format(debundler_tool),
        "--graph",
        "$(rlocationpath {})".format(graph_tree),
        "--graph-relpath",
        graph_relpath,
        "--modules-path",
        modules_path,
    ]
    data = [debundler_tool, graph_tree]
    graph_source_tree = None
    if graph_source_data:
        graph_source_tree = _checked_tree(name + "__relocatable_source", graph_source_data, testonly)
        data.append(graph_source_tree)

    if source_data:
        source_tree = _checked_tree(name + "__source", source_data, testonly)
        args.extend(["--source-root", "runfile", "$(rlocationpath {})".format(source_tree)])
        data.append(source_tree)
    elif graph_source_data:
        args.extend(["--source-root", "graph-source", source_root])
    else:
        args.extend(["--source-root", "workspace", source_root])

    if spec_root:
        args.extend(["--spec-root", spec_root])

    if graph_source_tree:
        args.extend(["--graph-source-root", "$(rlocationpath {})".format(graph_source_tree)])

    package_names = {}
    for index, (package_root, package_name) in enumerate(package_roots.items()):
        if package_name in package_names:
            fail("package_roots contains duplicate package name {!r}".format(package_name))
        package_names[package_name] = True

        root_label = native.package_relative_label(str(package_root))
        root_tree = _checked_tree(name + "__package_{}".format(index), root_label, testonly)
        args.extend([
            "--package-root",
            package_name,
            "$(rlocationpath {})".format(root_tree),
        ])
        data.append(root_tree)

    args.append("--")
    kwargs = {}
    if visibility != None:
        kwargs["visibility"] = visibility
    if tags != None:
        kwargs["tags"] = tags
    if testonly != None:
        kwargs["testonly"] = testonly

    sh_binary(
        name = name,
        srcs = [_LAUNCHER],
        args = [shell.quote(arg) for arg in args],
        data = data,
        use_bash_launcher = True,
        **kwargs
    )
