"""Pins which class of the cdk8s-generated tofu-controller bindings is the v1alpha2 CR.

The CRD serves v1alpha1 and v1alpha2 (storage); `cdk8s import` names the first listed
version's class plainly (`Terraform` is v1alpha1) and suffixes the rest, so every
generated CR goes through `TerraformV1Alpha2`.
"""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformV1Alpha2,
    TerraformV1Alpha2Spec,
    TerraformV1Alpha2SpecSourceRef,
    TerraformV1Alpha2SpecSourceRefKind,
)


def test_suffixed_class_renders_v1alpha2() -> None:
    chart = Cdk8sTesting.chart()
    TerraformV1Alpha2(
        chart,
        "terraform",
        spec=TerraformV1Alpha2Spec(
            interval="15m",
            source_ref=TerraformV1Alpha2SpecSourceRef(
                kind=TerraformV1Alpha2SpecSourceRefKind.GIT_REPOSITORY, name="repo"
            ),
        ),
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["apiVersion"] == "infra.contrib.fluxcd.io/v1alpha2"


if __name__ == "__main__":
    pytest_bazel.main()
