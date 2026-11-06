"""Each OpenTofu root's variables model against its module's variables.tf, by top-level name.

tofu only warns on a tfvars value the module does not declare: a variable renamed on one
side fails at plan time in the cluster when it is required, and silently falls back to its
default when it is not.
"""

from __future__ import annotations

import pygohcl
import pytest
import pytest_bazel
from pydantic import BaseModel
from python.runfiles import Runfiles

from cluster.cdk8s.cdviz.github_webhook import CdvizGithubWebhookVars
from cluster.cdk8s.forgejo_registry.chart import ForgejoImagesVars
from cluster.cdk8s.monitoring.home_switch import HomeSwitchVars
from cluster.cdk8s.route53_domain_delegation import Route53DomainDelegationVars


@pytest.mark.parametrize(
    ("model", "module"),
    [
        (Route53DomainDelegationVars, "route53-domain-delegation"),
        (ForgejoImagesVars, "forgejo-images"),
        (HomeSwitchVars, "home-switch"),
        (CdvizGithubWebhookVars, "cdviz-github-webhook"),
    ],
)
def test_vars_model_matches_module(runfiles: Runfiles, model: type[BaseModel], module: str) -> None:
    declared = pygohcl.loads((runfiles.root() / f"ducktape/tf/gitops/{module}/variables.tf").read_text())["variable"]
    assert (
        {name for name, spec in declared.items() if "default" not in spec} <= set(model.model_fields) <= set(declared)
    )


if __name__ == "__main__":
    pytest_bazel.main()
