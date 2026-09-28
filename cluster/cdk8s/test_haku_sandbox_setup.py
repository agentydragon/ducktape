"""`haku-sandbox-setup.sh`, an image build input, against the constructs that satisfy it.

The script stays hand-written, so its side of the agreement is read from it: bash's
`${VAR:?}` for what it requires.
"""

from __future__ import annotations

import re

import pytest
import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.haku import workspaces
from util.bazel.runfiles import get_required_path


@pytest.fixture(scope="module")
def script() -> str:
    return get_required_path("_main/cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh").read_text()


def test_haku_sandbox_sets_what_the_setup_requires(script: str) -> None:
    """The script writes ~/.netrc from `${HAKU_GIT_USERNAME:?}` / `${HAKU_GIT_PASSWORD:?}` and
    aborts the whole claim when either is unset -- at claim time, and silently otherwise.
    """
    required = set(re.findall(r"\$\{([A-Z_]+):\?", script))
    assert required, "the setup declares no required variables -- did the ${VAR:?} form change?"

    template = one(
        obj
        for obj in Cdk8sTesting.synth(workspaces.chart(Cdk8sTesting.app()))
        if obj["kind"] == "SandboxTemplate" and obj["metadata"]["name"] == workspaces.TEMPLATE_NAME
    )
    container = template["spec"]["podTemplate"]["spec"]["containers"][0]
    assert required <= {entry["name"] for entry in container["env"]}


if __name__ == "__main__":
    pytest_bazel.main()
