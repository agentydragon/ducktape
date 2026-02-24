"""Rule to compute a reproducible content hash of Bazel targets' outputs.

Used to produce a stamp-independent fingerprint of image content so CI can
skip pushing when sources haven't changed despite a new git commit (which
would otherwise change the stamped commit SHA embedded in the image).
"""

def _source_hash_impl(ctx):
    """Hash all output files from deps; produces a 64-char hex SHA256."""
    input_files = []
    for dep in ctx.attr.deps:
        input_files.extend(dep[DefaultInfo].files.to_list())

    # Sort by path for determinism across different Bazel action scheduling.
    input_files = sorted(input_files, key = lambda f: f.path)

    output = ctx.actions.declare_file(ctx.attr.name + ".txt")

    # sha256sum each input, then hash the combined output for a single digest.
    # $1 = output path; remaining args = input files.
    ctx.actions.run_shell(
        inputs = input_files,
        outputs = [output],
        arguments = [output.path] + [f.path for f in input_files],
        command = 'OUT="$1"; shift; sha256sum "$@" | sha256sum | awk \'{print $1}\' > "$OUT"',
    )

    return [DefaultInfo(files = depset([output]))]

source_hash = rule(
    implementation = _source_hash_impl,
    attrs = {
        "deps": attr.label_list(
            allow_files = True,
            doc = "Targets whose output files are hashed. Exclude stamp-dependent targets.",
        ),
    },
    doc = """Produces a content hash of the outputs of deps, independent of Bazel stamp.

Unlike comparing OCI image digests (which embed STABLE_BUILD_COMMIT), this
hash changes only when the actual file content of the deps changes. Safe to
use with --stamp builds: do not list //tools:build_info or any target that
transitively reads ctx.info_file here.
""",
)
