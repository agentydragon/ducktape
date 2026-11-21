"""The AT&T BGW320 gateway's device access code, which unlocks its login-only pages (NAT session
table, speed-test history). The SOPS Secret stays hand-written and only Flux decrypts it."""

from __future__ import annotations

from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.secret_ref import SecretRef

NAME = "att-gateway-exporter-secrets"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
# The Secret `access-code.sops.yaml` defines.
ACCESS_CODE = SecretRef(namespace="monitoring", name="att-gateway-exporter-access-code").key("access-code")
