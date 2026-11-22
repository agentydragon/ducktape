"""The upstream CRD imports Terraform as v1alpha2 and supports Flux sources."""

import pytest_bazel
from cdk8s import Chart
from tofu_controller.io.fluxcd.contrib.infra import (
    Terraform,
    TerraformSpec,
    TerraformSpecSourceRef,
    TerraformSpecSourceRefKind,
)


def test_terraform_renders_v1alpha2(scratch_chart: Chart) -> None:
    terraform = Terraform(
        scratch_chart,
        "terraform",
        spec=TerraformSpec(
            interval="15m",
            source_ref=TerraformSpecSourceRef(kind=TerraformSpecSourceRefKind.GIT_REPOSITORY, name="repo"),
        ),
    )
    assert terraform.api_version == "infra.contrib.fluxcd.io/v1alpha2"


if __name__ == "__main__":
    pytest_bazel.main()
