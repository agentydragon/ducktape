#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["huggingface_hub"]
# ///
"""Download thinking-capable models for vLLM on 2x RTX 5090."""

import os

from huggingface_hub import snapshot_download

# Use /wyrmhdd for storage
os.environ["HF_HOME"] = "/wyrmhdd/huggingface"

MODELS = [
    # Priority 1: Best reasoning + coding (fits on single GPU, ~17 GB)
    "casperhansen/deepseek-r1-distill-qwen-32b-awq",
    # Priority 2: Maximum quality (requires TP=2, ~38 GB)
    "casperhansen/deepseek-r1-distill-llama-70b-awq",
    # Priority 3: General Qwen3 with thinking (non-coder, ~17 GB)
    "Qwen/Qwen3-32B-AWQ",
]


def main():
    for model in MODELS:
        print(f"\n{'=' * 60}")
        print(f"Downloading: {model}")
        print(f"{'=' * 60}\n")
        try:
            path = snapshot_download(model)
            print(f"✓ Downloaded to: {path}")
        except Exception as e:
            print(f"✗ Failed: {e}")
            continue


if __name__ == "__main__":
    main()
