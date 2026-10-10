"""Each Terraform CR's variables model against its module's variables.tf, by top-level name.

tofu only warns on a tfvars value the module does not declare: a variable renamed on one
side fails at plan time in the cluster when it is required, and silently falls back to its
default when it is not.
"""

from __future__ import annotations

import pygohcl
import pytest
import pytest_bazel
from pydantic import BaseModel

from cluster.cdk8s.dns_automation import DnsRecordsVars
from cluster.cdk8s.forgejo_registry.chart import ForgejoImagesVars
from cluster.cdk8s.litellm.keys import KeysVars
from cluster.cdk8s.monitoring.home_switch import HomeSwitchVars
from util.bazel.runfiles import get_required_path


@pytest.mark.parametrize(
    ("model", "module"),
    [
        (DnsRecordsVars, "dns-records"),
        (ForgejoImagesVars, "forgejo-images"),
        (KeysVars, "litellm-keys"),
        (HomeSwitchVars, "home-switch"),
    ],
)
def test_vars_model_matches_module(model: type[BaseModel], module: str) -> None:
    declared = pygohcl.loads(get_required_path(f"_main/tf/gitops/{module}/variables.tf").read_text())["variable"]
    assert (
        {name for name, spec in declared.items() if "default" not in spec} <= set(model.model_fields) <= set(declared)
    )


if __name__ == "__main__":
    pytest_bazel.main()
