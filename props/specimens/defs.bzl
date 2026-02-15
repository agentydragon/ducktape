"""Bazel rules for specimen tar generation and testing."""

load("//tools/testing:docker.bzl", "docker_py_test")

def specimen_targets(name, slug):
    """Generate tar, metadata, and test targets for a specimen.

    Args:
        name: Base name for generated targets (typically "specimen")
        slug: Specimen slug in format "{repo}/{date}" (e.g., "ducktape/2026-01-17-00")

    Generates:
        - {name}_code_tar: tar.gz of code/ directory with BUILD.bazel files restored
        - {name}_metadata: filegroup of manifest.yaml + issues/**/*.yaml
        - test_{name}: docker_py_test validating this specimen
    """

    # Generate tar of code/ directory, renaming BUILD.bazel.specimen → BUILD.bazel
    native.genrule(
        name = name + "_code_tar",
        srcs = native.glob(
            ["code/**/*"],
            exclude = ["code/**/.git/**"],
        ),
        outs = [name + "_code.tar.gz"],
        cmd = """
set -euo pipefail
tmpdir=$$(mktemp -d)
trap 'rm -rf "$$tmpdir"' EXIT

# Write sources to file to avoid command line length issues
srcs_file="$$tmpdir/srcs.txt"
cat > "$$srcs_file" << 'SRCS_EOF'
$(SRCS)
SRCS_EOF

# Process each source file
while IFS= read -r src; do
    [[ -z "$$src" ]] && continue
    [[ ! -e "$$src" ]] && continue  # Skip files glob can't handle (special chars)

    rel="$${src#*code/}"
    [[ "$$rel" == "$$src" ]] && continue

    # Rename .specimen → original extension
    if [[ "$$src" == *.specimen ]]; then
        dest="$$tmpdir/$${rel%.specimen}"
    else
        dest="$$tmpdir/$$rel"
    fi

    mkdir -p "$$(dirname "$$dest")"
    cp "$$src" "$$dest"
done < <(tr ' ' '\\n' < "$$srcs_file")

# Create deterministic tar in tmpdir, then move to output
cd "$$tmpdir"
tar_name="$$(basename "$@")"
find . -type f | sort | tar \
    --create --gzip \
    --file="$$tar_name" \
    --mtime='1970-01-01 00:00:00' \
    --owner=0 --group=0 --numeric-owner \
    --no-recursion --files-from=-

mv "$$tar_name" "$$OLDPWD/$@"
        """,
        visibility = ["//visibility:public"],
    )

    # Metadata filegroup
    native.filegroup(
        name = name + "_metadata",
        srcs = ["manifest.yaml"] + native.glob(["issues/**/*.yaml"]),
        visibility = ["//visibility:public"],
    )

    # Per-specimen test
    docker_py_test(
        name = "test_" + name,
        srcs = [
            "//props/specimens:test_specimen.py",
        ],
        data = [
            ":" + name + "_code_tar",
            ":manifest.yaml",
        ] + native.glob(["issues/**/*.yaml"]),
        env = {
            "SPECIMEN_SLUG": slug,
            "SPECIMEN_CODE_TAR": "$(location :" + name + "_code_tar)",
            "SPECIMEN_MANIFEST": "$(location :manifest.yaml)",
            "SPECIMEN_ISSUES_DIR": native.package_name() + "/issues",
        },
        imports = ["../.."],
        tags = ["integration", "specimen"],
        deps = [
            "//bazel_util:runfiles",
            "//props:conftest",
            "//props/db:config",
            "//props/db:database",
            "//props/db:models",
            "//props/db:setup",
            "//props/db/sync",
            "//test_util:image_loader",
            "//third_party/containers:rlocations",
            "@pypi//pyhamcrest",
            "@pypi//pytest",
            "@pypi//pytest_bazel",
            "@pypi//sqlalchemy",
            "@pypi//testcontainers",
        ],
    )
