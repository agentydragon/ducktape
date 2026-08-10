"""Tests for the default-vpa-requests-only Kyverno policy.

Goldilocks VPA scales limits in proportion to requests, so a low steady-state
recommendation drags the limit down with it. On 2026-08-09 that admitted both
LiteLLM replicas at a 150m CPU limit against a declared 1; cold start could not
finish inside the startup probe budget and both crashlooped for six hours. The
policy defaults every auto-mode namespace to controlledValues: RequestsOnly so
VPA never sets a limit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy
from util.bazel.runfiles import get_required_path

VPA_POLICY_ANNOTATION = "goldilocks.fairwinds.com/vpa-resource-policy"
VPA_UPDATE_MODE_LABEL = "goldilocks.fairwinds.com/vpa-update-mode"


@pytest.fixture
def vpa_policy() -> Path:
    return policy("default-vpa-requests-only.yaml")


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path("_main/cluster/k8s/kustomization.yaml").parent


def _declared_policy(ns: dict) -> dict | None:
    raw = (ns["metadata"].get("annotations") or {}).get(VPA_POLICY_ANNOTATION)
    return json.loads(raw) if raw else None


class TestPolicyBehaviour:
    def test_auto_namespace_gets_requests_only(self, vpa_policy: Path):
        result = apply_policy(vpa_policy, manifest("namespace_vpa_auto.yaml"))
        assert result.ok, result.stdout
        [ns] = result.mutated_resources
        declared = _declared_policy(ns)
        assert declared is not None, f"auto-mode namespace was not annotated\n{result.stdout}"
        assert [c["controlledValues"] for c in declared["containerPolicies"]] == ["RequestsOnly"]
        assert [c["containerName"] for c in declared["containerPolicies"]] == ["*"]

    def test_own_policy_is_never_overwritten(self, vpa_policy: Path):
        """The +() anchor adds only when absent, so an explicit policy survives."""
        result = apply_policy(vpa_policy, manifest("namespace_vpa_auto_with_own_policy.yaml"))
        assert result.ok, result.stdout
        for ns in result.mutated_resources:
            declared = _declared_policy(ns)
            assert declared == {
                "containerPolicies": [{"containerName": "pinned", "controlledValues": "RequestsAndLimits"}]
            }, f"the namespace's own policy was modified\n{result.stdout}"

    def test_non_auto_namespace_untouched(self, vpa_policy: Path):
        """VPA never mutates running pods outside auto mode, so there is nothing to default."""
        result = apply_policy(vpa_policy, manifest("namespace_vpa_initial_mode.yaml"))
        assert result.ok, result.stdout
        for ns in result.mutated_resources:
            assert _declared_policy(ns) is None, f"non-auto namespace was annotated\n{result.stdout}"


def test_declared_policies_set_controlled_values(k8s_dir: Path) -> None:
    """An auto-mode namespace with its own policy must spell out controlledValues.

    The policy only adds the annotation when it is absent, so declaring any
    policy — even a partial one setting nothing but minAllowed — opts the
    namespace out of the default entirely and silently leaves VPA scaling
    limits. airlock was exactly that case before this test existed.
    """
    offenders: list[str] = []
    for path in k8s_dir.rglob("*.yaml"):
        if path.name.endswith(".sops.yaml"):
            continue
        text = path.read_text()
        # Gate on content: Namespace objects are not confined to namespace.yaml
        # (namespace-patch.yaml, gotk-components.yaml and some HelmReleases
        # declare them too), and parsing every YAML here would pull in Authentik
        # blueprints with their custom tags.
        if "kind: Namespace" not in text:
            continue
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict) or doc.get("kind") != "Namespace":
                continue
            meta = doc.get("metadata") or {}
            if (meta.get("labels") or {}).get(VPA_UPDATE_MODE_LABEL) != "auto":
                continue
            declared = _declared_policy(doc)
            if declared is None:
                continue  # the Kyverno default applies
            offenders += [
                f"{path.relative_to(k8s_dir)}: {meta.get('name')} containerName="
                f"{container.get('containerName')!r} has no controlledValues"
                for container in declared["containerPolicies"]
                if "controlledValues" not in container
            ]
    assert not offenders, "\n".join(
        ["Auto-mode namespaces declaring their own VPA policy must set controlledValues:", *offenders]
    )


if __name__ == "__main__":
    pytest_bazel.main()
