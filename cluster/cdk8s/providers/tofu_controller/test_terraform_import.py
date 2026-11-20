"""The upstream CRD imports Terraform as v1alpha2 and supports Flux sources."""

import pytest_bazel
from cdk8s import (
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from tofu_controller.io.fluxcd.contrib.infra import (
    Terraform,
    TerraformSpec,
    TerraformSpecSourceRef,
    TerraformSpecSourceRefKind,
)


def test_terraform_renders_v1alpha2(scratch_chart: Chart) -> None:
    Terraform(
        scratch_chart,
        "terraform",
        spec=TerraformSpec(
            interval="15m",
            source_ref=TerraformSpecSourceRef(kind=TerraformSpecSourceRefKind.GIT_REPOSITORY, name="repo"),
        ),
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    assert manifest["apiVersion"] == "infra.contrib.fluxcd.io/v1alpha2"


if __name__ == "__main__":
    pytest_bazel.main()
