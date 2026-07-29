"""Pin every `codex-gpt-5.6-*` declaration in the repo to the measured window.

The numbers below are measured, not published, and the published ones are wrong
in both directions: the raw models are ~1.05M, Codex product documentation says
272K, and the serving path this repo actually uses (OpenClaw -> LiteLLM ->
CLIProxyAPI -> upstream) accepts neither. LiteLLM carries no `max_input_tokens`
for these routes, so nothing upstream of this file can be consulted instead.

`openai_utils/probe_context_window.py` binary-searches the live path. On
2026-07-29 all three 5.6 models rejected identically:

    accepted 370,629 counted tokens / rejected 372,194 counted tokens

so the total context is 372,000. Re-run the probe to re-derive it:

    kubectl exec -i -n <ns> <pod> -- python3 - --low 350000 --high 400000 \\
        codex-gpt-5.6-{luna,sol,terra} < openai_utils/probe_context_window.py

This is not a change-detector test asserting a literal equals itself: the point
is that several *independent* manifests must agree with each other. Before this
test, `openclawinstance.yaml` and the public-coder-agent ConfigMap had silently
drifted apart, and both were wrong.
"""

import json
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

# Measured 2026-07-29; see module docstring for provenance and how to re-derive.
CONTEXT_WINDOW = 372_000
MAX_TOKENS = 128_000

MODEL_PREFIX = "codex-gpt-5.6-"

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def _openclaw_model_entries(k8s_dir: Path) -> list[tuple[str, str, dict]]:
    """Every declared model entry, as (source, model id, entry)."""
    entries: list[tuple[str, str, dict]] = []

    instance = k8s_dir / "agents/openclaw/gateway/openclawinstance.yaml"
    instance_providers = yaml.safe_load(instance.read_text())["spec"]["config"]["raw"]["models"]["providers"]
    entries.extend(
        (instance.name, model["id"], model)
        for provider in instance_providers.values()
        for model in provider.get("models", [])
    )

    configmap = k8s_dir / "agents/public-coder-agent/app/openclaw-config.yaml"
    config = json.loads(yaml.safe_load(configmap.read_text())["data"]["openclaw.json"])
    entries.extend(
        (configmap.name, model["id"], model)
        for provider in config["models"]["providers"].values()
        for model in provider.get("models", [])
    )

    return entries


def test_declarations_exist(k8s_dir: Path) -> None:
    """Guard the guard: a rename that empties the search would pass everything else."""
    entries = _openclaw_model_entries(k8s_dir)
    sources = {source for source, model_id, _ in entries if model_id.startswith(MODEL_PREFIX)}
    assert len(sources) >= 2, (
        f"expected {MODEL_PREFIX}* declarations in at least two manifests, found {sources}. "
        "If a manifest moved, update _openclaw_model_entries rather than deleting coverage."
    )


def test_context_window_matches_measurement(k8s_dir: Path) -> None:
    wrong = [
        f"{source}:{model_id} declares contextWindow={entry.get('contextWindow')}"
        for source, model_id, entry in _openclaw_model_entries(k8s_dir)
        if model_id.startswith(MODEL_PREFIX) and entry.get("contextWindow") != CONTEXT_WINDOW
    ]
    assert not wrong, (
        f"contextWindow must be the measured {CONTEXT_WINDOW:,} for every {MODEL_PREFIX}* model:\n"
        + "\n".join(f"  {w}" for w in wrong)
    )


def test_max_tokens_matches_measurement(k8s_dir: Path) -> None:
    wrong = [
        f"{source}:{model_id} declares maxTokens={entry.get('maxTokens')}"
        for source, model_id, entry in _openclaw_model_entries(k8s_dir)
        if model_id.startswith(MODEL_PREFIX) and entry.get("maxTokens") != MAX_TOKENS
    ]
    assert not wrong, f"maxTokens must be {MAX_TOKENS:,} for every {MODEL_PREFIX}* model:\n" + "\n".join(
        f"  {w}" for w in wrong
    )


def test_output_reservation_fits_the_window() -> None:
    """maxTokens is reserved out of the window, so it cannot exceed it."""
    assert MAX_TOKENS < CONTEXT_WINDOW, (
        f"maxTokens ({MAX_TOKENS:,}) must leave room for input inside the {CONTEXT_WINDOW:,} window."
    )


if __name__ == "__main__":
    pytest_bazel.main()
