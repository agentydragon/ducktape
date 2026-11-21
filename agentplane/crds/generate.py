"""Go-derived Agentplane CRDs, written by `bb run //agentplane/crds:generate_bin`.

Sandbox, EgressCredential, policy, identity and Kubernetes account CRDs are generated from Go API types by
controller-gen. Flux packages the CRDs directly from
`agentplane/crds/manifests` (`cluster/cdk8s/agentplane_crds.py`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import yaml
from python.runfiles import Runfiles

from devinfra.bazel.workspace import get_build_workspace_directory

CRDS_DIR = Path("agentplane/crds/manifests")
SANDBOX_GENERATED_CRDS = "ducktape/agentplane/sandboxes/api/generated_crds"
EGRESS_GENERATED_CRDS = "ducktape/agentplane/egress/api/v1alpha1/generated_crds"
POLICY_GENERATED_CRDS = "ducktape/agentplane/policy/api/v1alpha1/generated_crds"
WORKLOAD_IDENTITY_GENERATED_CRDS = "ducktape/agentplane/identity/credentials/api/v1alpha1/generated_crds"
# Each CRD file in `CRDS_DIR` -> runfiles directory holding the controller-gen output it is rendered from.
GENERATED_CRDS = {
    "crd-egresscredentials.yaml": EGRESS_GENERATED_CRDS,
    "crd-policysets.yaml": POLICY_GENERATED_CRDS,
    "crd-policybindings.yaml": POLICY_GENERATED_CRDS,
    "crd-identities.yaml": POLICY_GENERATED_CRDS,
    "crd-kubernetesrolesets.yaml": POLICY_GENERATED_CRDS,
    "crd-serviceaccountgrants.yaml": POLICY_GENERATED_CRDS,
    "crd-managedserviceaccounts.yaml": POLICY_GENERATED_CRDS,
    "crd-sandboxtemplates.yaml": SANDBOX_GENERATED_CRDS,
    "crd-sandboxes.yaml": SANDBOX_GENERATED_CRDS,
    "crd-workloadcredentials.yaml": WORKLOAD_IDENTITY_GENERATED_CRDS,
}
CRD_FILES = tuple(GENERATED_CRDS)


def _render_crd(generated: str) -> str:
    crd = cast(dict[str, Any], yaml.safe_load(generated))
    annotations = crd["metadata"].get("annotations", {})
    annotations.pop("controller-gen.kubebuilder.io/version", None)
    if not annotations:
        crd["metadata"].pop("annotations", None)
    for version in crd["spec"]["versions"]:
        if version.get("subresources") == {}:
            del version["subresources"]
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


def render_crds(runfiles_root: Path) -> Iterator[tuple[Path, str]]:
    """(repo-relative path, content) of each CRD rendered from its controller-gen output."""
    for name, directory in GENERATED_CRDS.items():
        # controller-gen names its output `<group>_<plural>.yaml`; the committed file is `crd-<plural>.yaml`.
        source = runfiles_root / directory / f"agentplane.allegedly.works_{name.removeprefix('crd-')}"
        yield CRDS_DIR / name, _render_crd(source.read_text())


def main() -> None:
    root = get_build_workspace_directory()
    for relative, content in render_crds(Runfiles.CreateOrRaise().root()):
        (root / relative).write_text(content)


if __name__ == "__main__":
    main()
