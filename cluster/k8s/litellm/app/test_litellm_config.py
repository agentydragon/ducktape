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


def test_agentplane_staging_offers_all_native_subscription_models() -> None:
    config = yaml.safe_load(get_required_path("ducktape/cluster/k8s/agentplane-staging/app/config.yaml").read_text())
    tf_locals = _litellm_keys_locals()
    assert config["models"] == {"codex": tf_locals["oai_lane_models"], "claude": tf_locals["claude_client_models"]}
    for preset in config["thread_presets"].values():
        assert preset["model"] in config["models"][preset["provider"]]


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


# The shape segment names the wire LiteLLM speaks upstream (model_rosters.py), so the name and
# the wiring must agree on both halves. The definer half must match the handler
# `litellm_params.model`'s prefix selects -- the check that catches naming a Google-wire entry
# `oai-chat`, or an Ollama-native one. The protocol half is pinned by `model_info.mode`, which is
# what separates two shapes sharing a definer (oai-chat vs oai-responses, goog-generate vs
# goog-embed). A provider absent from this map has not declared which wire it speaks, so adding
# one is a deliberate edit rather than a silent pass.
_UPSTREAM_DEFINER = {
    "anthropic": "ant",
    "openai": "oai",
    "mistral": "oai",  # OpenAI-compatible chat at api.mistral.ai
    "groq": "oai",  # OpenAI-compatible chat at api.groq.com/openai/v1
    "gemini": "goog",
    "ollama": "olm",
}
_SHAPE_MODE = {
    ApiShape.ANT_MESSAGES: "chat",
    ApiShape.OAI_CHAT: "chat",
    ApiShape.OAI_RESPONSES: "responses",
    ApiShape.GOOG_GENERATE: "chat",
    ApiShape.GOOG_EMBED: "embedding",
    ApiShape.OLM_CHAT: "chat",
    ApiShape.OLM_EMBED: "embedding",
}


def test_shape_segment_matches_each_entry_upstream_wire() -> None:
    scheme_entries = [
        entry for entry in _load_config("proxy-config.yaml")["model_list"] if entry["model_name"].count("/") == 2
    ]
    shapes_seen = set()
    for entry in scheme_entries:
        name = entry["model_name"]
        shape = ApiShape(name.split("/")[1])
        shapes_seen.add(shape)
        upstream = entry["litellm_params"]["model"].split("/")[0]
        assert _UPSTREAM_DEFINER[upstream] == shape.partition("-")[0], name
        assert entry["model_info"]["mode"] == _SHAPE_MODE[shape], name
    assert shapes_seen == set(ApiShape)


def test_config_maps_mount_their_matching_committed_configs() -> None:
    kustomization = yaml.safe_load(get_required_path("ducktape/cluster/k8s/litellm/app/kustomization.yaml").read_text())
    config_files = {config["name"]: config["files"] for config in kustomization["configMapGenerator"]}
    assert config_files == {"litellm-config": ["config.yaml=proxy-config.yaml"]}


if __name__ == "__main__":
    pytest_bazel.main()
