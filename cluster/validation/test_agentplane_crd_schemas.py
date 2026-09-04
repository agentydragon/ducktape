"""The kubeconform schemas under cluster/schemas/ are the Agentplane CRDs' openAPIV3Schema.

The pre-commit kubeconform hook validates EgressPolicy, EgressBinding and EgressCredential manifests against
`cluster/schemas/<group>/<kind>_<version>.json`; each file is generated from its CRD here and
pinned, so an edit to a CRD that is not mirrored fails this test rather than letting the
hook accept manifests the API server would reject.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_CRD_FILES = [
    get_required_path("_main/cluster/k8s/agentplane-crds/crd-egresspolicies.yaml"),
    get_required_path("_main/cluster/k8s/agentplane-crds/crd-egressbindings.yaml"),
    get_required_path("_main/cluster/k8s/agentplane-crds/crd-egresscredentials.yaml"),
]
_SCHEMAS_DIR = get_required_path("_main/cluster/schemas")


def _versions(crd_file: Path) -> list[tuple[Path, object]]:
    crd = yaml.safe_load(crd_file.read_text())
    group = crd["spec"]["group"]
    kind = crd["spec"]["names"]["kind"].lower()
    return [
        (_SCHEMAS_DIR / group / f"{kind}_{version['name']}.json", version["schema"]["openAPIV3Schema"])
        for version in crd["spec"]["versions"]
    ]


@pytest.mark.parametrize(
    ("schema_file", "expected"),
    [entry for crd_file in _CRD_FILES for entry in _versions(crd_file)],
    ids=lambda value: value.name if isinstance(value, Path) else "",
)
def test_schema_file_is_crd_schema(schema_file: Path, expected: object) -> None:
    assert json.loads(schema_file.read_text()) == expected, (
        f"{schema_file.name} differs from its CRD; regenerate it by dumping the version's openAPIV3Schema as JSON"
    )


if __name__ == "__main__":
    pytest_bazel.main()
