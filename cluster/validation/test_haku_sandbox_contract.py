"""Contracts for Haku sandbox and egress deployment wiring."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest_bazel
import yaml
from more_itertools import one


def sandbox_env(template: dict[str, object]) -> dict[str, dict[str, Any]]:
    container = cast(dict[str, Any], template["spec"]["podTemplate"]["spec"]["containers"][0])  # type: ignore[index]
    return {entry["name"]: entry for entry in container.get("env", [])}


def test_haku_sandbox_satisfies_the_shared_bootstrap(k8s_dir: Path) -> None:
    """`haku-sandbox-setup.sh` writes ~/.netrc from `${HAKU_GIT_USERNAME:?}` / `${HAKU_GIT_PASSWORD:?}`
    and aborts the whole claim when either is unset. That is the right behaviour and exactly why
    it wants a test: the failure lands at claim time, silently otherwise.
    """
    script = (k8s_dir / "haku/workspaces/image/haku-sandbox-setup.sh").read_text()
    required = set(re.findall(r"\$\{([A-Z_]+):\?", script))
    assert required, "the bootstrap declares no required variables — did the ${VAR:?} form change?"

    template = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku.yaml").read_text())
    assert required <= set(sandbox_env(template))


def test_claude_sandbox_can_reach_the_forgejo_the_bootstrap_clones_from(k8s_dir: Path) -> None:
    """The clone target and the egress policy that permits it must not drift apart."""
    script = (k8s_dir / "haku/workspaces/image/haku-sandbox-setup.sh").read_text()
    url = one(re.findall(r"HAKU_STATE_URL:-http://([a-z0-9-]+)\.([a-z0-9-]+):(\d+)/", script))
    _, namespace, port = url

    egress = yaml.safe_load((k8s_dir / "agents/haku-egress-proxy/ccnp-haku-agent-egress.yaml").read_text())
    allowed = {
        (rule["toEndpoints"][0]["matchLabels"]["k8s:io.kubernetes.pod.namespace"], ports["port"])
        for rule in egress["spec"]["egress"]
        if "toEndpoints" in rule
        for entry in rule.get("toPorts", [])
        for ports in entry["ports"]
    }
    assert (namespace, port) in allowed


def test_haku_sandbox_reaches_kubernetes_only_through_console(k8s_dir: Path) -> None:
    """The haku-sandbox exec target reaches Kubernetes only through the Console-mediated proxy."""
    binding = yaml.safe_load((k8s_dir / "haku/rbac/rolebinding-haku.yaml").read_text())
    role = yaml.safe_load((k8s_dir / "haku/rbac/role.yaml").read_text())
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    subjects = {(s["kind"], s["name"], s.get("namespace")) for s in binding["subjects"]}
    # The ServiceAccount subject stays: ordinary pods that do carry a credential — the
    # managed-agent worker — run as it. What must hold is that the group Console SARs for a
    # proxied request resolves to that same Role, so mediating access never widens or narrows it.
    assert subjects == {("ServiceAccount", "haku", "haku-sandbox"), ("Group", "haku:access-profile:haku", None)}

    exec_target = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku.yaml").read_text())
    pod = exec_target["spec"]["podTemplate"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod

    # Removing the mount and supplying the proxy are one decision: a box with neither has no
    # path to the API at all, and would fail at `kubectl` rather than at deploy.
    exec_env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
    assert "haku-kube-api-proxy" in exec_env["HAKU_KUBERNETES_PROXY_URL"]


if __name__ == "__main__":
    pytest_bazel.main()
