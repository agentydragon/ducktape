"""`haku-sandbox-setup.sh`, an image build input, against the constructs that satisfy it.

The script stays hand-written, so its side of each agreement is read from it: bash's
`${VAR:?}` for what it requires, and the `HAKU_STATE_URL` default for where it clones from.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import haku_egress_proxy
from cluster.cdk8s.haku import workspaces
from util.bazel.runfiles import get_required_path


@pytest.fixture(scope="module")
def script() -> str:
    return get_required_path("_main/cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh").read_text()


def _object(objects: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(obj for obj in objects if obj["kind"] == kind and obj["metadata"]["name"] == name)


def test_haku_sandbox_sets_what_the_setup_requires(script: str) -> None:
    """The script writes ~/.netrc from `${HAKU_GIT_USERNAME:?}` / `${HAKU_GIT_PASSWORD:?}` and
    aborts the whole claim when either is unset -- at claim time, and silently otherwise.
    """
    required = set(re.findall(r"\$\{([A-Z_]+):\?", script))
    assert required, "the setup declares no required variables -- did the ${VAR:?} form change?"

    template = _object(
        Cdk8sTesting.synth(workspaces.chart(Cdk8sTesting.app())), "SandboxTemplate", workspaces.TEMPLATE_NAME
    )
    container = template["spec"]["podTemplate"]["spec"]["containers"][0]
    assert required <= {entry["name"] for entry in container["env"]}


def test_agent_runner_can_reach_the_forgejo_the_setup_clones_from(script: str) -> None:
    """The clone target and the egress policy that permits it must not drift apart."""
    _, namespace, port = one(re.findall(r"HAKU_STATE_URL:-http://([a-z0-9-]+)\.([a-z0-9-]+):(\d+)/", script))

    egress = _object(
        Cdk8sTesting.synth(haku_egress_proxy.chart(Cdk8sTesting.app())),
        "CiliumClusterwideNetworkPolicy",
        "haku-agent-runner-egress",
    )
    allowed = {
        (rule["toEndpoints"][0]["matchLabels"]["k8s:io.kubernetes.pod.namespace"], ports["port"])
        for rule in egress["spec"]["egress"]
        if "toEndpoints" in rule
        for entry in rule.get("toPorts", [])
        for ports in entry["ports"]
    }
    assert (namespace, port) in allowed


if __name__ == "__main__":
    pytest_bazel.main()
