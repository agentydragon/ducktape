"""One contract over every proxy-injection policy in the cluster.

Three Kyverno policies inject proxy configuration into three sandbox namespaces.
They are separate files because there are three proxies, not because the three
sandboxes want different treatment — so the interesting property is the one
nothing enforced before: **they inject the same set of variables**.

That set is not arbitrary. A pod told to use a proxy but not told where the
proxy's CA lives fails TLS on its first request, and the four CA variables exist
because four client stacks read four different things (OpenSSL, curl, Python
`requests`, Node). Injecting a subset is the failure mode this pins: it does not
break at admission, it breaks later, in whichever runtime happens to use the
client whose variable went missing.

The policies are run through the real `kyverno` CLI rather than parsed, so what
is asserted is the mutation Kyverno actually performs — including that the
`initContainers` block, which is a hand-duplicated copy of the `containers`
block, did not drift from it. Kyverno's RFC 6902 patches cannot share one env
list across the two, so that duplication is structural and this is what keeps the
copies equal.

Values differ per policy and are declared per policy. `NO_PROXY` is declared as a
union of named groups rather than a string, so that what the policies share and
what they deliberately do not is legible instead of buried in a diff between long
comma-separated strings. All three carry `CLUSTER_ADDRESSING`; the two groups
that stay haku-only are named with the reason they cannot be unified.

Not covered here: the deployments and Jobs that set the same variables by hand
(`haku-openclaw-spike`, `public-coder-agent`, `haku-ci`, the agent-sdk smoke
Job). They are not Kyverno policies, they diverge more widely than these three,
and at least one — `public-coder-agent`, which sets `NODE_EXTRA_CA_CERTS` but no
`REQUESTS_CA_BUNDLE` — looks like it has the gap described above. TODO: extend
this contract to them once the injection-vs-hand-rolled split is settled.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy, apply_twice, assert_not_mutated
from cluster.validation.kyverno.paths import manifest
from util.bazel.runfiles import get_required_path

# What every proxy-injection policy must inject. Split into the two halves it is
# made of, since a policy carrying one half and not the other is the specific
# defect being caught.
PROXY_VARS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})
# One per client stack that ignores the others: OpenSSL/Python-ssl, curl,
# `requests`, Node.
CA_VARS = frozenset({"SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"})
INJECTED = PROXY_VARS | CA_VARS

# `NO_PROXY` groups. Suffix matching is literal and the syntax differs across
# curl, Go, Python, node and git, which is why several of these carry the same
# name in more than one form.
LOOPBACK = frozenset({"127.0.0.1", "localhost"})

# Every way a pod addresses something inside the cluster. Carried by all three
# policies: in-cluster traffic is deliberately unfenced, so there is no reason
# for one sandbox to bypass less of it than another. The short Service forms are
# listed because `.svc.cluster.local` does not match them — without them a client
# that builds `https://kubernetes.default.svc`, which is what in-cluster
# libraries do by default, reaches the proxy and hangs until timeout.
CLUSTER_ADDRESSING = frozenset({".svc", ".svc.cluster.local", "kubernetes.default.svc", "10.0.0.0/8"})

# The one group that is NOT shared, and must not be: `*.allegedly.works` would
# break `claude-sandbox`'s docker-ci path, which is supposed to go *through* the
# proxy to be raw-tunnelled. In-cluster Forgejo is bypassed by neither: haku's
# sandbox carries a credential placeholder that only the fence can redeem.
OWN_DOMAINS = frozenset({"*.allegedly.works", ".allegedly.works"})


@dataclass(frozen=True)
class Injection:
    """What one policy injects, and into which namespace."""

    namespace: str
    proxy: str
    ca_bundle: str
    # `NO_PROXY`, as the union of the named groups above.
    bypasses: frozenset[str]


POLICIES: dict[str, Injection] = {
    "cluster/k8s/kyverno/policies/inject-haku-egress-proxy.yaml": Injection(
        namespace="haku-sandbox",
        proxy="http://haku-egress-proxy.haku-egress-proxy.svc.cluster.local:8080",
        ca_bundle="/egress-proxy-ca/ca-certificates.crt",
        bypasses=LOOPBACK | CLUSTER_ADDRESSING | OWN_DOMAINS,
    ),
    "cluster/k8s/kyverno/policies/inject-mitmproxy.yaml": Injection(
        namespace="claude-sandbox",
        proxy="http://mitmproxy.agents-mitmproxy.svc.cluster.local:8080",
        ca_bundle="/mitmproxy-ca/ca-certificates.crt",
        bypasses=LOOPBACK | CLUSTER_ADDRESSING,
    ),
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
def mutated(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Injection, list[dict]]:
    """Every container of the probe pod, after the policy under test ran."""
    injection = POLICIES[request.param]
    resource = tmp_path / "pod.yaml"
    resource.write_text(_pod(injection.namespace))
    result = apply_policy(get_required_path(f"_main/{request.param}"), resource)
    assert result.ok, result.stdout
    pod = next(doc for doc in result.mutated_resources if doc["kind"] == "Pod")
    return injection, [*pod["spec"]["initContainers"], *pod["spec"]["containers"]]


@pytest.fixture(params=sorted(POLICIES), ids=lambda p: Path(p).stem)
def reinvoked(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    """The probe pod after the policy ran **twice** — one CREATE, two Kyverno passes.

    This is not a hypothetical. Kyverno's mutating webhook is registered
    `reinvocationPolicy: IfNeeded`, so when a webhook ordered after it mutates the pod,
    Kyverno runs again on the same CREATE and sees its own output as input.
    """
    injection = POLICIES[request.param]
    policy = get_required_path(f"_main/{request.param}")

    resource = tmp_path / "pod.yaml"
    resource.write_text(_pod(injection.namespace))
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
    (cluster/k8s/agents/public-coder-agent/app/deployment.yaml) — carries exactly this wiring, so
    a widening of this policy's namespace match to its namespace would skip it rather than stamp
    port-8080 values over its iron-proxy env.
    """
    assert_not_mutated(
        get_required_path("_main/cluster/k8s/kyverno/policies/inject-haku-egress-proxy.yaml"),
        manifest("pod_self_wired_egress_proxy.yaml"),
    )


def _env(container: dict) -> dict[str, str]:
    return {entry["name"]: entry["value"] for entry in container["env"]}


def test_policy_injects_the_whole_variable_set(mutated: tuple[Injection, list[dict]]) -> None:
    """Same variables in every policy, and in both container kinds.

    Equality, not containment: a policy that stops injecting one of these leaves
    pods that negotiate TLS against a bundle they were never told about.
    """
    _, containers = mutated
    for container in containers:
        assert set(_env(container)) == INJECTED, container["name"]


def test_ca_variables_agree_on_one_bundle_path(mutated: tuple[Injection, list[dict]]) -> None:
    """The four CA variables describe one file, so they must name the same one."""
    injection, containers = mutated
    for container in containers:
        env = _env(container)
        assert {env[name] for name in CA_VARS} == {injection.ca_bundle}, container["name"]


def test_both_proxy_urls_point_at_the_declared_proxy(mutated: tuple[Injection, list[dict]]) -> None:
    """HTTP and HTTPS go to the same proxy: the fence has one chokepoint, not two."""
    injection, containers = mutated
    for container in containers:
        env = _env(container)
        assert {env["HTTP_PROXY"], env["HTTPS_PROXY"]} == {injection.proxy}, container["name"]


def test_no_proxy_is_exactly_the_declared_groups(mutated: tuple[Injection, list[dict]]) -> None:
    """Everything listed is reached with no proxy and no egress allowlist applied.

    Accepted because every group is cluster addressing or a cluster-published
    name, and those services authenticate their own callers — but a new entry is
    a new hole in the fence, so it has to join a named group to get in.
    """
    injection, containers = mutated
    for container in containers:
        actual = {entry.strip() for entry in _env(container)["NO_PROXY"].split(",")}
        assert actual == set(injection.bypasses), container["name"]


if __name__ == "__main__":
    pytest_bazel.main()
