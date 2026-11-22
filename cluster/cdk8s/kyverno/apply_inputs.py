"""Fixture lookups and focused synthesis for the Kyverno CLI tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from cdk8s import (
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from python.runfiles import Runfiles


def manifest(name: str) -> Path:
    """An input resource from this package's testdata directory."""
    return Runfiles.CreateOrRaise().root() / f"ducktape/cluster/cdk8s/kyverno/testdata/{name}"


def policy(chart: Chart, directory: Path, *, json_mode: bool = False) -> Path:
    """Synthesize a single-policy chart to a file for the real Kyverno CLI.

    `json_mode` rewrites a `ValidatingPolicy` to `evaluation.mode: JSON` without its
    `matchConstraints`, so the CLI evaluates the same CEL `validations` against a JSON payload
    (`apply_policy(..., json_payload=True)`). Admission matching is not exercised.
    """
    [document] = Cdk8sTesting.synth(chart)
    if json_mode:
        document["spec"]["evaluation"] = {"mode": "JSON"}
        del document["spec"]["matchConstraints"]
    path = directory / "policy.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path
