"""Verify push-images.yml matrix matches the set of ghcr_push-tagged oci_image targets.

Two invariants:
  1. Every oci_image target tagged ghcr_push must have a matrix row in push-images.yml.
  2. Every matrix row's `image:` value must correspond to a ghcr_push-tagged target.

Violations of invariant 1 mean a new image was wired up in Bazel but never added to
the push workflow.  Violations of invariant 2 mean a workflow row references a target
that either doesn't exist or isn't tagged for pushing (likely a stale/typo row).
"""

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_QUERY_RLOC = "_main/devinfra/ci/ghcr_push_images_query"
_WORKFLOW_RLOC = "_main/.github/workflows/push-images.yml"


def _load_tagged_targets(query_file: Path) -> set[str]:
    """Read the genquery output — one Bazel label per line."""
    return {line.strip() for line in query_file.read_text().splitlines() if line.strip()}


def _load_matrix_labels(workflow_file: Path) -> set[str]:
    """Extract `image:` values from the push-images.yml strategy matrix."""
    data = yaml.safe_load(workflow_file.read_text())
    include = data["jobs"]["push"]["strategy"]["matrix"]["include"]
    return {entry["image"] for entry in include if "image" in entry}


def _normalize(label: str) -> str:
    """Normalize a Bazel label to canonical //pkg:target form."""
    if ":" in label:
        return label
    pkg = label.lstrip("/")
    name = pkg.rsplit("/", 1)[-1]
    return f"//{pkg}:{name}"


def test_matrix_completeness() -> None:
    tagged = {_normalize(t) for t in _load_tagged_targets(get_required_path(_QUERY_RLOC))}
    matrix = {_normalize(m) for m in _load_matrix_labels(get_required_path(_WORKFLOW_RLOC))}

    missing_from_matrix = tagged - matrix
    extra_in_matrix = matrix - tagged

    errors: list[str] = []
    if missing_from_matrix:
        errors.append(
            "oci_image targets tagged ghcr_push but missing from push-images.yml matrix:\n"
            + "\n".join(f"  {t}" for t in sorted(missing_from_matrix))
        )
    if extra_in_matrix:
        errors.append(
            "push-images.yml matrix rows not matched by any ghcr_push-tagged target:\n"
            + "\n".join(f"  {t}" for t in sorted(extra_in_matrix))
            + '\n(add tags = ["ghcr_push"] to the target, or remove the matrix row)'
        )

    if errors:
        pytest.fail("\n\n".join(errors))


if __name__ == "__main__":
    pytest_bazel.main()
