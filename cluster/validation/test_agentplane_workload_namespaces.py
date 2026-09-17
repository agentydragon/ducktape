"""A bearer the central proxy admits is one the LLM ingress admits too.

A workload's token is authenticated twice on its way to a model: the proxy resolves it, decides the
request against its bindings, and substitutes it as the credential the ingress then resolves again.
Both read the same allowlist setting, so a namespace on the proxy's list and missing from the
ingress's is admitted for one hop and refused with 401 on the next -- a misconfiguration no single
manifest looks wrong in. This asserts the relation between the two rather than either one's roster,
so adding a workload namespace to both stays silent and adding it to one fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path

# Both services name their settings `Settings`, and this test parses one of each.
from x.agentplane.egress.main import Settings as EgressSettings
from x.agentplane.llm_ingress.main import Settings as IngressSettings

NAMESPACES = ["agentplane-staging", "agentplane-testing"]
# The settings each Deployment supplies as an environment variable rather than a flag.
DATABASE_URL = "--database-url=postgresql://validation-test/validation-test"
LITELLM_KEY = "--litellm-key=validation-test-not-a-key"


def _container_args(namespace: str, path: str, container: str) -> list[str]:
    manifest = get_required_path(f"_main/cluster/k8s/{namespace}/{path}")
    pod: dict[str, Any] = yaml.safe_load(Path(manifest).read_text())["spec"]["template"]["spec"]
    return list(one(candidate for candidate in pod["containers"] if candidate["name"] == container)["args"])


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_the_ingress_admits_every_namespace_the_proxy_does(namespace: str) -> None:
    proxy = EgressSettings(
        _cli_parse_args=[*_container_args(namespace, "egress/deployment-agentplane-egress.yaml", "proxy"), DATABASE_URL]
    )
    ingress = IngressSettings(
        _cli_parse_args=[*_container_args(namespace, "llm-ingress/deployment.yaml", "ingress"), LITELLM_KEY]
    )

    assert proxy.allowed_service_account_namespaces <= ingress.allowed_service_account_namespaces, (
        "the proxy authenticates a workload and sends it to the ingress, which authenticates the same "
        "bearer again, so a namespace the proxy admits and the ingress does not is refused mid-hop"
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_both_read_the_same_projected_token_audience(namespace: str) -> None:
    """The substituted credential is the workload's own token, so one audience has to satisfy both."""
    proxy = EgressSettings(
        _cli_parse_args=[*_container_args(namespace, "egress/deployment-agentplane-egress.yaml", "proxy"), DATABASE_URL]
    )
    ingress = IngressSettings(
        _cli_parse_args=[*_container_args(namespace, "llm-ingress/deployment.yaml", "ingress"), LITELLM_KEY]
    )

    assert proxy.token_audience == ingress.token_audience


if __name__ == "__main__":
    pytest_bazel.main()
