"""Bazel-driven debundle pipeline rule.

Generates the transform spec with `--out-root` pointing at a tree-artifact
output directory, then runs the ducktape debundler against that spec. All
pipeline outputs (manifests, analysis, emitted JS) land under the rule's
declared output directory in `bazel-bin/`. Each corpus may layer a
`write_source_files` regen target on top to commit a subset of outputs into
the source tree.
"""

load("@bazel_skylib//lib:paths.bzl", "paths")
load("@bazel_skylib//lib:shell.bzl", "shell")

def _debundle_pipeline_impl(ctx):
    out_dir = ctx.actions.declare_directory(ctx.label.name + ".out")
    spec = ctx.actions.declare_file(ctx.label.name + ".spec.jsonc")
    bin_dir = ctx.bin_dir.path

    # Generate the spec. The js_binary chdirs into BAZEL_BINDIR before
    # invoking node, so the script sees its CLI paths as bin-dir-relative.
    # `short_path` of a declared output is exactly that bin-dir-relative
    # form for both the spec file and the tree-artifact output dir.
    ctx.actions.run(
        executable = ctx.executable.spec_generator,
        outputs = [spec],
        arguments = [
            "--out",
            spec.short_path,
            "--out-root",
            out_dir.short_path,
        ],
        env = {"BAZEL_BINDIR": bin_dir},
        progress_message = "Generating debundle spec for %{label}",
        mnemonic = "DebundleSpecGen",
    )

    # The spec records inputs (snapshot/, js-files.txt, asset-summary.json)
    # and the output dir as bin-dir-relative paths. Run the debundler under
    # `cd $BAZEL_BINDIR` so those paths resolve against the
    # aspect_rules_js-materialized `bazel-out/<config>/bin/...` tree.
    debundler_args = ["--spec", spec.short_path]
    for pkg_label, pkg_name in ctx.attr.package_roots.items():
        pkg_files = pkg_label[DefaultInfo].files.to_list()
        if not pkg_files:
            fail("package_roots entry {} has no files".format(pkg_name))

        # The `:dir` filegroup is a single tree artifact whose `.path`
        # already points directly at the package directory containing
        # `package.json`. Make it bin-dir-relative for the post-cd cwd.
        pkg_dir = paths.relativize(pkg_files[0].path, bin_dir)
        debundler_args.append("--package-root")
        debundler_args.append("{}={}".format(pkg_name, pkg_dir))

    inputs = depset(
        direct = [spec],
        transitive = [dep[DefaultInfo].files for dep in ctx.attr.input_data] +
                     [pkg[DefaultInfo].files for pkg in ctx.attr.package_roots.keys()],
    )

    # `${OLDPWD}` keeps a reference to the execroot so we can address the
    # debundler binary (sitting under bazel-out for tools) after `cd`.
    ctx.actions.run_shell(
        inputs = inputs,
        tools = [ctx.executable.debundler],
        outputs = [out_dir],
        command = "cd \"${{BAZEL_BINDIR}}\" && exec \"${{OLDPWD}}/{debundler}\" {args}".format(
            debundler = ctx.executable.debundler.path,
            args = " ".join([shell.quote(a) for a in debundler_args]),
        ),
        env = {"BAZEL_BINDIR": bin_dir},
        # The debundler asserts that each vendor package's resolved subpath
        # canonicalizes to a location within the package root. Inside
        # Bazel's linux-sandbox, package-dir entries are real directories
        # but their leaf files are symlinks to the host execroot's bazel-bin
        # — so `realpath(file)` lands outside `realpath(dir)` and the check
        # spuriously fails. Disable sandboxing for this action; inputs are
        # declared via Bazel attrs, so reproducibility is preserved.
        execution_requirements = {"no-sandbox": "1"},
        progress_message = "Running debundle pipeline for %{label}",
        mnemonic = "DebundlePipeline",
    )

    # Default output is just the tree artifact so consumers (and shell-arg
    # `$(rlocationpath ...)` expansion) get a single file label. The spec
    # JSON is exposed via the `spec` output group for ad-hoc inspection.
    return [
        DefaultInfo(files = depset([out_dir])),
        OutputGroupInfo(spec = depset([spec])),
    ]

debundle_pipeline = rule(
    implementation = _debundle_pipeline_impl,
    attrs = {
        "spec_generator": attr.label(
            executable = True,
            cfg = "exec",
            mandatory = True,
            doc = "js_binary that emits the transform spec; must accept `--out <path>` and `--out-root <dir>`.",
        ),
        "debundler": attr.label(
            executable = True,
            cfg = "exec",
            mandatory = True,
            doc = "Debundler binary; must accept `--spec <path>` and `--package-root <name>=<dir>`.",
        ),
        "input_data": attr.label_list(
            allow_files = True,
            doc = "Source-tree inputs the spec references (extracted/, snapshots/).",
        ),
        "package_roots": attr.label_keyed_string_dict(
            allow_files = True,
            doc = "Vendor package roots: label of the package's `:dir` filegroup → package name. The first file's dirname is passed as `--package-root <name>=<dir>`.",
        ),
    },
)
