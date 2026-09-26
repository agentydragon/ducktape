"""Behaviour every proxy-injection policy shares, run through the real `kyverno` CLI.

Both policies come from `cluster/cdk8s/kyverno/proxy_injection.py`, which builds one env
list per policy for both container kinds; what is pinned here is what Kyverno does with the
resulting RFC 6902 patches: a second pass over the same CREATE adds nothing, and a pod that
already carries the wiring is skipped.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_twice, assert_not_mutated
from cluster.validation.kyverno.paths import manifest
from util.bazel.runfiles import get_required_path

# Policy manifest -> the sandbox namespace it matches.
POLICIES = {
    "cluster/generated/kyverno/policies/inject-haku-egress-proxy.k8s.yaml": "haku-sandbox",
    "cluster/generated/kyverno/policies/inject-mitmproxy.k8s.yaml": "claude-sandbox",
}


def _pod(namespace: str) -> str:
    """A pod with both container kinds, so both patch blocks are exercised."""
    return textwrap.dedent(f"""
        apiVersion: v1
        kind: Pod
        metadata:
          name: proxy-injection-probe
          namespace: {namespace}
        spec:
          initContainers:
            - name: setup
              image: curlimages/curl:latest
          containers:
            - name: app
              image: curlimages/curl:latest
        """)


@pytest.fixture(params=sorted(POLICIES), ids=lambda p: Path(p).stem)
def reinvoked(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    """The probe pod after the policy ran **twice** — one CREATE, two Kyverno passes.

    This is not a hypothetical. Kyverno's mutating webhook is registered
    `reinvocationPolicy: IfNeeded`, so when a webhook ordered after it mutates the pod,
    Kyverno runs again on the same CREATE and sees its own output as input.
    """
    policy = get_required_path(f"_main/{request.param}")

    resource = tmp_path / "pod.yaml"
    resource.write_text(_pod(POLICIES[request.param]))
    _, second = apply_twice(policy, resource, tmp_path)
    return next(d for d in second.mutated_resources if d["kind"] == "Pod")


def _dupes(names: list[str]) -> list[str]:
    return sorted({n for n in names if names.count(n) > 1})


def test_injection_is_idempotent_under_reinvocation(reinvoked: dict) -> None:
    """Injecting twice must equal injecting once.

    The patches are RFC 6902 `add` against `/-`, which appends unconditionally. Applied a
    second time they append a second copy, and a duplicate volume name or mountPath is
    rejected outright by the API server:

        Pod "haku-ui-0" is invalid: [spec.volumes[3].name: Duplicate value:
        "haku-egress-proxy-ca-cert", spec.containers[0].volumeMounts[3].mountPath:
        Invalid value: "/egress-proxy-ca": must be unique]

    That is a real outage, seen 2026-08-05: `haku-ui` sat at 1/2 with `haku-ui-0`
    permanently uncreatable, because the namespace runs VPA in `Auto` mode and VPA's
    webhook mutates resource requests *after* Kyverno — triggering the reinvocation. It is
    intermittent only because VPA patches nothing when its recommendation already matches
    the pod, which is what let the surviving replica in.

    Duplicate `env` entries are legal (last wins) and so cannot be caught at admission,
    but they are the same defect and are asserted here too.
    """
    spec = reinvoked["spec"]
    assert _dupes([v["name"] for v in spec["volumes"]]) == []
    for container in [*spec["initContainers"], *spec["containers"]]:
        assert _dupes([m["name"] for m in container["volumeMounts"]]) == [], container["name"]
        assert _dupes([m["mountPath"] for m in container["volumeMounts"]]) == [], container["name"]
        assert _dupes([e["name"] for e in container["env"]]) == [], container["name"]


def test_pod_carrying_its_own_wiring_is_left_alone() -> None:
    """A pod that already holds the policy's proxy wiring is skipped whole, env included.

    Every rule preconditions on the thing it appends being absent — the volume rule on the CA
    volume, the env-and-mount rules on the CA volumeMount — so a pod template that ships the
    complete wiring itself gets no injection at all. That is what lets a self-wired pod keep its
    own `HTTP_PROXY`: env is last-entry-wins, so an appended fleet value would otherwise override
    the pod's. The #4943 spike target — the public-coder-agent OpenClaw pod
    (cluster/cdk8s/public_coder_agent_config.py) — carries exactly this wiring, so
    a widening of this policy's namespace match to its namespace would skip it rather than stamp
    port-8080 values over its iron-proxy env.
    """
    assert_not_mutated(
        get_required_path("_main/cluster/generated/kyverno/policies/inject-haku-egress-proxy.k8s.yaml"),
        manifest("pod_self_wired_egress_proxy.yaml"),
    )


if __name__ == "__main__":
    pytest_bazel.main()
