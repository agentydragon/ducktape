"""haku-forgejo-tea: the SOPS-encrypted Forgejo API token Reflector mirrors into haku-ci's KEDA
scaler. The Secret stays hand-written."""

from __future__ import annotations

from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "haku-forgejo-tea"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/haku/forgejo-tea"
