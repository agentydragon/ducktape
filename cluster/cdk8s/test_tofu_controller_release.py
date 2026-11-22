"""The installed controller and CRD must accept the Terraform resources GitOps creates."""

import pytest_bazel
from cdk8s import App, Chart

from cluster.cdk8s import terraform, tofu_controller_release


def test_controller_accepts_gitops_terraform(scratch_app: App, scratch_chart: Chart) -> None:
    resource = terraform.gitops_terraform(
        scratch_chart, "terraform", module=terraform.gitops_module("sample"), variables=None
    )
    objects = tofu_controller_release.chart(scratch_app).to_json()
    (release,) = (obj for obj in objects if obj["kind"] == "HelmRelease")
    (definition,) = (obj for obj in objects if obj["kind"] == "CustomResourceDefinition")
    assert release["spec"]["values"]["featureGates"]["externalArtifact"] is True
    assert definition["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"

    spec = definition["spec"]
    assert spec["names"]["kind"] == resource.kind
    version = next(
        v for v in spec["versions"] if v["served"] and f"{spec['group']}/{v['name']}" == resource.api_version
    )
    source_ref = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["sourceRef"]
    assert resource.to_json()["spec"]["sourceRef"]["kind"] == "ExternalArtifact"
    assert "ExternalArtifact" in source_ref["properties"]["kind"]["enum"]


if __name__ == "__main__":
    pytest_bazel.main()
