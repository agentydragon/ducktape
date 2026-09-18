"""Verify that model-roster consumers agree with the shared catalog."""

import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import staging_config
from cluster.cdk8s.model_rosters import ANTHROPIC_MODELS, CLIPROXY_MODELS, ApiShape, Provider, exposed_name
from cluster.validation.terraform_hcl import locals_blocks
from util.bazel.runfiles import get_required_path


def _model_key_locals() -> dict:
    blocks = locals_blocks(get_required_path("ducktape/tf/gitops/litellm-keys/main.tf"))
    return one(blocks)


# The Codex-subscription names appear in three places: CLIPROXY_MODELS
# (model_rosters.py), and `oai_lane_models` + `codex_client_models` in
# tf/gitops/litellm-keys/main.tf, which scope the virtual keys. A comment in
# that file asks the lists to be kept in sync; these pin it instead, so adding
# a Codex model cannot half-land and leave a key allowlisting one that does
# not exist (or omitting one that does).
def test_terraform_codex_allowlists_match_the_model_roster() -> None:
    tf_locals = _model_key_locals()
    assert tf_locals["oai_lane_models"] == [
        exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model) for model in CLIPROXY_MODELS
    ]
    assert tf_locals["codex_client_models"] == [
        exposed_name(Provider.CHATGPT, ApiShape.ANT_MESSAGES, model) for model in CLIPROXY_MODELS
    ]


# The Claude-subscription names live in ANTHROPIC_MODELS (model_rosters.py) and
# `claude_client_models` in tf/gitops/litellm-keys/main.tf, which scopes the
# haku-console-claude runner key. This pins the two in sync, so a new Claude
# model cannot half-land and leave the key policy out of sync with the catalog.
def test_terraform_claude_allowlist_matches_the_model_roster() -> None:
    tf_locals = _model_key_locals()
    assert tf_locals["claude_client_models"] == [
        exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model) for model in ANTHROPIC_MODELS
    ]


def test_agentplane_staging_offers_all_native_subscription_models() -> None:
    config = staging_config.config()
    tf_locals = _model_key_locals()
    assert config["models"] == {
        "HARNESS_CLAUDE": tf_locals["claude_client_models"],
        "HARNESS_CODEX": tf_locals["oai_lane_models"],
    }
    for preset in config["thread_presets"].values():
        assert preset["model"] in config["models"][preset["harness"]]


if __name__ == "__main__":
    pytest_bazel.main()
