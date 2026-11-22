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


def policy(chart: Chart, directory: Path) -> Path:
    """Synthesize a single-policy chart to a file for the real Kyverno CLI."""
    [document] = Cdk8sTesting.synth(chart)
    path = directory / "policy.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path
