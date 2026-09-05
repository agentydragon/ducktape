"""Verify cross-file LiteLLM configuration wiring."""

import pytest_bazel
import yaml
from more_itertools import one

from cluster.k8s.litellm.app.model_rosters import ANTHROPIC_MODELS, CLIPROXY_MODELS, ApiShape, Provider, exposed_name
from cluster.validation.terraform_hcl import locals_blocks
from util.bazel.runfiles import get_required_path


def _load_config(filename: str) -> dict:
    loaded = yaml.safe_load(get_required_path(f"ducktape/cluster/k8s/litellm/app/{filename}").read_text())
    assert isinstance(loaded, dict)
    return loaded


# The Codex-subscription names appear in three places: CLIPROXY_MODELS (model_rosters.py),
# and `oai_lane_models` + `codex_client_models` in tf/gitops/litellm-keys/main.tf, which
# scope the virtual keys. A comment in that file asks the lists to be kept in sync; these
# pin it instead, so adding a Codex model cannot half-land and leave a key allowlisting a
# model that does not exist (or omitting one that does).
def _litellm_keys_locals() -> dict:
    blocks = locals_blocks(get_required_path("ducktape/tf/gitops/litellm-keys/main.tf"))
    return one(blocks)


def test_terraform_codex_allowlists_match_the_cliproxy_model_list() -> None:
    tf_locals = _litellm_keys_locals()
    assert tf_locals["oai_lane_models"] == [
        exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model) for model in CLIPROXY_MODELS
    ]
    assert tf_locals["codex_client_models"] == [
        exposed_name(Provider.CHATGPT, ApiShape.ANT_MESSAGES, model) for model in CLIPROXY_MODELS
    ]


# The Claude-subscription names live in ANTHROPIC_MODELS (model_rosters.py) and `claude_client_models`
# in tf/gitops/litellm-keys/main.tf, which scopes the haku-console-claude runner key. This pins the
# two in sync, so a new Claude model cannot half-land and leave the key allowlisting one the proxy
# does not serve (or omitting one it does).
def test_terraform_claude_allowlist_matches_the_anthropic_model_list() -> None:
    tf_locals = _litellm_keys_locals()
    assert tf_locals["claude_client_models"] == [
        exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model) for model in ANTHROPIC_MODELS
    ]


# main.tf's own comment: "Model names must match generated model_name entries in
# cluster/k8s/litellm/app/proxy-config.yaml". These are the remaining live-key
# locals that spell names out literally, so every element must resolve against the
# committed config rather than a second hand-maintained model reconstruction.
_TF_LITERAL_MODEL_LOCALS = [
    "oai_lane_models",
    "tana_client_models",
    "codex_client_models",
    "claude_client_models",
    "embedding_client_models",
    "gemini_client_models",
    "cheap_experiments_models",
]


def test_terraform_key_allowlists_only_name_models_the_proxy_serves() -> None:
    served = {entry["model_name"] for entry in _load_config("proxy-config.yaml")["model_list"]}
    tf_locals = _litellm_keys_locals()
    for local in _TF_LITERAL_MODEL_LOCALS:
        missing = [model for model in tf_locals[local] if model not in served]
        assert not missing, f"{local} allows models the proxy does not serve: {missing}"


def test_hidden_model_aliases_target_served_models() -> None:
    config = _load_config("proxy-config.yaml")
    served = {entry["model_name"] for entry in config["model_list"]}
    aliases = config["router_settings"]["model_group_alias"]

    assert aliases["gpt-6-astra"] == {"model": "chatgpt/oai-responses/gpt-6-astra", "hidden": True}
    assert all(alias["model"] in served for alias in aliases.values())


# haku-console picks its Codex chat runtime's model from Git YAML — the one Codex consumer
# whose model choice lives outside the baked-config and Terraform pins above. The runner
# hardcodes wire_api="responses" (haku/runner/codex/options.py), so the model
# must be a Responses-wire entry; a Messages-wire name fails every turn at /v1/responses
# (haku/console/x/codex_app_server/testdata/real_provider_failure.sanitized.jsonl).
def test_console_codex_harnesses_use_oai_responses_wire_models() -> None:
    config = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text())
    responses_wire_names = {exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model) for model in CLIPROXY_MODELS}
    for name, runtime in config["harnesses"].items():
        implementation = runtime["implementation"]
        if implementation["kind"] == "codex_app_server":
            assert implementation["model"] in responses_wire_names, name


# haku-console's Claude runtime (#4670) picks model + haiku_model from Git YAML, the same
# outside-the-Terraform-pins spot as the Codex runtime above. Claude Code speaks the Anthropic
# Messages wire against CLIProxyAPI's Claude subscription, so both must be
# anthropic-max20/ant-messages/*
# entries the proxy serves -- never the codex chatgpt/ant-messages/* lane a stale GPT guess would
# name, which the haku-console-claude key does not admit and which is a broken model turn every
# request. Admits any served claude-lane model, so it pins the wire without hardcoding the choice.
def test_console_claude_harness_uses_claude_ant_messages_wire_models() -> None:
    config = yaml.safe_load(get_required_path("ducktape/cluster/k8s/haku/console/config.yaml").read_text())
    claude_wire_names = {
        exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model) for model in ANTHROPIC_MODELS
    }
    for name, runtime in config["harnesses"].items():
        implementation = runtime["implementation"]
        if implementation["kind"] == "claude_code":
            assert implementation["model"] in claude_wire_names, name
            assert implementation["haiku_model"] in claude_wire_names, name


def test_config_maps_mount_their_matching_committed_configs() -> None:
    kustomization = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/kustomization.yaml").read_text())
    config_files = {config["name"]: config["files"] for config in kustomization["configMapGenerator"]}
    assert config_files == {"litellm-config": ["config.yaml=proxy-config.yaml"]}


if __name__ == "__main__":
    pytest_bazel.main()
