"""Go-derived Agentplane CRDs, written by `bb run //agentplane/crds:generate_bin`.

Sandbox, EgressCredential and PolicyBinding CRDs are generated from Go API types by controller-gen.
PolicySet remains authored in `manifests/`. Flux packages the CRDs directly from
`agentplane/crds/manifests` (`cluster/cdk8s/agentplane_crds.py`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory

CRDS_DIR = Path("agentplane/crds/manifests")
CRD_FILES = (
    "crd-egresscredentials.yaml",
    "crd-policysets.yaml",
    "crd-policybindings.yaml",
    "crd-sandboxtemplates.yaml",
    "crd-sandboxes.yaml",
)
SANDBOX_CRDS = {
    "crd-sandboxtemplates.yaml": "agentplane.allegedly.works_sandboxtemplates.yaml",
    "crd-sandboxes.yaml": "agentplane.allegedly.works_sandboxes.yaml",
}
EGRESS_CRDS = {"crd-egresscredentials.yaml": "agentplane.allegedly.works_egresscredentials.yaml"}
SANDBOX_GENERATED_CRDS = "_main/agentplane/sandboxes/api/generated_crds"
EGRESS_GENERATED_CRDS = "_main/agentplane/egress/api/v1alpha1/generated_crds"
POLICY_GENERATED_CRDS = "_main/agentplane/policy/api/v1alpha1/generated_crds"
POLICY_CRDS = {"crd-policybindings.yaml": "agentplane.allegedly.works_policybindings.yaml"}


def _generated_crd(generated: str) -> dict[str, Any]:
    crd = cast(dict[str, Any], yaml.safe_load(generated))
    annotations = crd["metadata"].get("annotations", {})
    annotations.pop("controller-gen.kubebuilder.io/version", None)
    if not annotations:
        crd["metadata"].pop("annotations", None)
    for version in crd["spec"]["versions"]:
        if version.get("subresources") == {}:
            del version["subresources"]
    return crd


def _sandbox_crd(generated: str) -> str:
    crd = _generated_crd(generated)
    if crd["spec"]["names"]["kind"] == "SandboxTemplate":
        claim = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"][
            "workspaceVolume"
        ]["properties"]["claim"]
        # controller-gen expands the upstream PVC spec, whose Go type is needed by API consumers.
        # Keep only the one nested field our CEL rule reads. The API server preserves the rest of
        # the claim as opaque data, as it did in the authored CRD.
        volume_mode = claim["properties"]["volumeMode"]
        claim.clear()
        claim.update(
            type="object", **{"x-kubernetes-preserve-unknown-fields": True}, properties={"volumeMode": volume_mode}
        )
    return yaml.safe_dump(crd, sort_keys=False)


def generated_files(
    crds: Mapping[str, str],
    sandbox_crds: Mapping[str, str],
    egress_crds: Mapping[str, str],
    policy_crds: Mapping[str, str],
) -> Iterator[tuple[Path, str]]:
    """(repo-relative path, content) of each generated CRD."""
    for crd_file, committed in crds.items():
        if crd_file in SANDBOX_CRDS:
            text = _sandbox_crd(sandbox_crds[crd_file])
            yield CRDS_DIR / crd_file, text
        elif crd_file in EGRESS_CRDS:
            generated = egress_crds[crd_file]
            text = yaml.safe_dump(_generated_crd(generated), sort_keys=False)
            yield CRDS_DIR / crd_file, text
        elif crd_file in POLICY_CRDS:
            text = yaml.safe_dump(_generated_crd(policy_crds[crd_file]), sort_keys=False)
            yield CRDS_DIR / crd_file, text
        else:
            yield CRDS_DIR / crd_file, committed


def main() -> None:
    root = get_build_workspace_directory()
    sandbox_generated = get_required_path(SANDBOX_GENERATED_CRDS)
    egress_generated = get_required_path(EGRESS_GENERATED_CRDS)
    sandbox_crds = {name: (sandbox_generated / source).read_text() for name, source in SANDBOX_CRDS.items()}
    egress_crds = {name: (egress_generated / source).read_text() for name, source in EGRESS_CRDS.items()}
    policy_generated = get_required_path(POLICY_GENERATED_CRDS)
    policy_crds = {name: (policy_generated / source).read_text() for name, source in POLICY_CRDS.items()}
    for relative, content in generated_files(
        {name: (root / CRDS_DIR / name).read_text() for name in CRD_FILES}, sandbox_crds, egress_crds, policy_crds
    ):
        (root / relative).write_text(content)


if __name__ == "__main__":
    main()
