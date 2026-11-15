"""Format a JS source tree with Prettier while retaining all its other files."""

load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_run_binary")

def _validate_relative_path(value, field, allow_empty = False):
    if allow_empty and value == "":
        return
    if value.startswith("/") or "\\" in value:
        fail("{} must be a relative POSIX path: {!r}".format(field, value))
    for segment in value.split("/"):
        if segment in ["", ".", ".."]:
            fail("{} must not contain empty, '.' or '..' path segments: {!r}".format(field, value))

def prettier_js_tree(
        name,
        src,
        out_dir = None,
        input_subdir = "",
        prettier = Label("//:node_modules/prettier"),
        prettier_config = Label("//:prettierrc"),
        visibility = None):
    """Format every `.js` file in a tree and copy every other file byte-for-byte.

    Args:
      name: Name of the output target.
      src: A single target that provides an input directory tree artifact.
      out_dir: Output directory path relative to this package's bin directory.
        Defaults to `name`.
      input_subdir: Optional relative directory inside `src` to format. When set,
        that directory's contents become the output tree root.
      prettier: Target that makes the caller's pinned `prettier` npm package
        available to a caller-package `js_binary`. Defaults to Ducktape's pin.
      prettier_config: Single-file target for the Prettier configuration.
        Defaults to Ducktape's configuration.
      visibility: Visibility of the output target.

    The `src` and `prettier_config` labels are declared action inputs. `prettier`
    is a runtime dependency of the helper binary, so external callers can use
    their own npm workspace's Prettier package and configuration.
    """
    _validate_relative_path(input_subdir, "input_subdir", allow_empty = True)
    if out_dir == None:
        out_dir = name
    _validate_relative_path(out_dir, "out_dir")

    runner_name = "_{}_prettier_runner".format(name.replace("/", "_"))
    js_binary(
        name = runner_name,
        data = [prettier],
        entry_point = Label("//devinfra/js:prettier_js_tree.mjs"),
        no_copy_to_bin = [Label("//devinfra/js:prettier_js_tree.mjs")],
        visibility = ["//visibility:private"],
    )

    src_label = str(src)
    config_label = str(prettier_config)
    js_run_binary(
        name = name,
        srcs = [src, prettier_config],
        args = [
            "$(location {})".format(src_label),
            "$(RULEDIR)/{}".format(out_dir),
            "$(location {})".format(config_label),
            input_subdir,
        ],
        copy_srcs_to_bin = False,
        out_dirs = [out_dir],
        tool = ":" + runner_name,
        use_execroot_entry_point = False,
        visibility = visibility,
    )
