#!/usr/bin/env bash
# Wrapper for telethon-cli that injects api_id/api_hash from SOPS-encrypted
# secrets/shared/telegram-api.yaml (decrypted via ~/.ssh/id_ed25519 / SOPS_AGE_KEY).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET="$REPO_ROOT/secrets/shared/telegram-api.yaml"

read -r api_id api_hash < <(
  sops -d "$SECRET" | python3 -c "
import yaml, sys
d = yaml.safe_load(sys.stdin)
print(d['api_id'], d['api_hash'])
"
)

exec uv run --with telethon-cli telethon-cli \
  "$@" \
  --api-id "$api_id" \
  --api-hash "$api_hash"
