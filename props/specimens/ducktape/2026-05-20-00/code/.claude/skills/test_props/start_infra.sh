#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STATE_DIR="$REPO_ROOT/props/.devenv/state"
PASSWORD_FILE="$STATE_DIR/pg_password"

# Generate PostgreSQL password if not exists.
# Use hex-only (no /, +, = chars) to avoid asyncpg DSN parsing issues.
mkdir -p "$STATE_DIR"
if [ ! -f "$PASSWORD_FILE" ]; then
  echo "Generating PostgreSQL password..."
  openssl rand -hex 16 >"$PASSWORD_FILE"
  chmod 600 "$PASSWORD_FILE"
fi

export PG_PASSWORD
PG_PASSWORD=$(cat "$PASSWORD_FILE")

echo "=== Props Infrastructure Startup ==="
docker compose -f "$SCRIPT_DIR/compose.yaml" up -d --wait

echo ""
echo "=== Infrastructure Ready ==="
echo "PostgreSQL:   127.0.0.1:5433"
echo "OCI Registry: 127.0.0.1:5050"
echo ""
echo "Environment variables to set:"
echo "  export PGHOST=127.0.0.1"
echo "  export PGPORT=5433"
echo "  export PGUSER=postgres"
echo "  export PGPASSWORD=\$(cat $PASSWORD_FILE)"
echo "  export PGDATABASE=eval_results"
echo ""
echo "To stop: docker compose -f $SCRIPT_DIR/compose.yaml down"
