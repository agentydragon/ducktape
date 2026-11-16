"""Apply the BuildBuddy credential used by the LiteLLM live-test key sync."""

from __future__ import annotations

from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/keys-tf"
