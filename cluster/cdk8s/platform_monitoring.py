"""The Flux Kustomization applying the cluster's scrape targets and alert rules that no
application's own unit carries. Each module builds its own chart; `generate_manifests.py` writes
them all into `OUTPUT_DIR`."""

from __future__ import annotations

from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "platform-monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/platform-monitoring"
