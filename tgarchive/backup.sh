#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SECRET="$REPO_ROOT/secrets/shared/telegram-api.yaml"

read -r api_id api_hash < <(
  sops -d "$SECRET" | python3 -c "
import yaml, sys
d = yaml.safe_load(sys.stdin)
print(d['api_id'], d['api_hash'])
"
)

exec "$SCRIPT_DIR/tg_backup.py" \
  --api-id "$api_id" \
  --api-hash "$api_hash" \
  "$@"
