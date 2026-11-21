"""Fixture lookups and focused synthesis for the Kyverno CLI tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from python.runfiles import Runfiles

from cluster.cdk8s.kyverno import policies

_POLICY_BUILDERS = {
    "require-gitops": policies.require_gitops_chart,
    "default-disable-service-links": policies.default_disable_service_links_chart,
    "require-secret-store-conditions": policies.require_secret_store_conditions_chart,
}


_POLICY_KINDS = {"MutatingPolicy", "ValidatingPolicy"}


def manifest(name: str) -> Path:
    """An input resource from this package's testdata directory."""
    return Runfiles.CreateOrRaise().root() / f"ducktape/cluster/cdk8s/kyverno/testdata/{name}"


def policy(app: App, name: str, directory: Path, *, json_mode: bool = False) -> Path:
    """Synthesize one policy constructor to a file for the real Kyverno CLI.

    `json_mode` rewrites a `ValidatingPolicy` to `evaluation.mode: JSON` without its
    `matchConstraints`, so the CLI evaluates the same CEL `validations` against a JSON payload
    (`apply_policy(..., json_payload=True)`). Admission matching is not exercised.
    """
    chart = _POLICY_BUILDERS[name](app)
    matches = [
        doc for doc in Cdk8sTesting.synth(chart) if doc["kind"] in _POLICY_KINDS and doc["metadata"]["name"] == name
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one policy named {name!r}, got {len(matches)}")
    document = matches[0]
    if json_mode:
        document["spec"]["evaluation"] = {"mode": "JSON"}
        del document["spec"]["matchConstraints"]
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path
