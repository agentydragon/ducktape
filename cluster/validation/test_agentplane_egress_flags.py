"""The flags the egress Deployments pass are flags the egress binary accepts.

A renamed or dropped setting is otherwise found by a CrashLoopBackOff. `workload_namespaces` is a
set, and how pydantic-settings takes a set on the command line is not obvious from the field, so the
manifests and the parser are checked against each other rather than each against a remembered
spelling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one
from pydantic import ValidationError

from util.bazel.runfiles import get_required_path
from x.agentplane.egress.main import Settings

NAMESPACES = ["agentplane-staging", "agentplane-testing"]
# The one setting the manifests supply as an environment variable rather than a flag.
DATABASE_URL = "--database-url=postgresql://validation-test/validation-test"


def _proxy_args(namespace: str) -> list[str]:
    manifest = get_required_path(f"_main/cluster/k8s/{namespace}/egress/deployment-agentplane-egress.yaml")
    pod: dict[str, Any] = yaml.safe_load(Path(manifest).read_text())["spec"]["template"]["spec"]
    return list(one(container for container in pod["containers"] if container["name"] == "proxy")["args"])


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_the_deployed_flags_parse_into_settings(namespace: str) -> None:
    settings = Settings(_cli_parse_args=[*_proxy_args(namespace), DATABASE_URL])

    assert settings.rules_namespace == namespace
    assert namespace in settings.workload_namespaces, (
        "the sandboxes run beside the rules, so their namespace has to be one a bearer may come from"
    )


def test_a_second_workload_namespace_is_another_comma() -> None:
    """What hosting an agent elsewhere costs: one more name on the flag, not a JSON array."""
    without = [arg for arg in _proxy_args("agentplane-staging") if not arg.startswith("--workload-namespaces=")]
    settings = Settings(
        _cli_parse_args=[*without, "--workload-namespaces=agentplane-staging,public-coder", DATABASE_URL]
    )

    assert settings.workload_namespaces == frozenset({"agentplane-staging", "public-coder"})


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_a_deployment_naming_no_workload_namespace_is_refused(namespace: str) -> None:
    """The allowlist is what lets a bearer be presented at all, so an empty one accepts nothing and
    is a misconfiguration to fail on at startup rather than serve."""
    without = [arg for arg in _proxy_args(namespace) if not arg.startswith("--workload-namespaces=")]

    with pytest.raises(ValidationError, match="workload_namespaces"):
        Settings(_cli_parse_args=[*without, DATABASE_URL])


if __name__ == "__main__":
    pytest_bazel.main()
