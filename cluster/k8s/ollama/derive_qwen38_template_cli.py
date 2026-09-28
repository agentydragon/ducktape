"""Bazel entry point for the pinned Qwen3.8 metadata-shard derivation."""

from cluster.k8s.ollama.patch_qwen38_template import main

if __name__ == "__main__":
    main()
