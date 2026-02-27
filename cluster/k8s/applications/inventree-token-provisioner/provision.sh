#!/bin/bash
set -euo pipefail

SECRET_NAME="inventree-api-token"
INVENTREE_NS="inventree"
INVENTREE_SVC="http://inventree:8000"

# Idempotency: Reflector handles syncing to sandbox namespaces;
# just check the source secret in the inventree namespace.
if kubectl get secret "$SECRET_NAME" -n "$INVENTREE_NS" >/dev/null 2>&1; then
  echo "Secret $SECRET_NAME already exists in $INVENTREE_NS — done."
  exit 0
fi

# Find a running InvenTree pod to exec into.
echo "Finding a running InvenTree pod..."
until POD=$(kubectl get pod -n "$INVENTREE_NS" \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) && [ -n "$POD" ]; do
  echo "  No running pod found, retrying in 10s..."
  sleep 10
done
echo "Using pod: $POD"

# Wait for the InvenTree API to be accepting requests.
echo "Waiting for InvenTree API..."
until curl -sf "$INVENTREE_SVC/api/" >/dev/null 2>&1; do
  echo "  API not ready, retrying in 5s..."
  sleep 5
done
echo "InvenTree API is ready."

# Create or update the sandbox-agent user via Django ORM.
# SANDBOX_PASSWORD is alphanumeric-only (Terraform special=false) — safe to embed.
# The exec'd Python process inherits the container's environment, so
# DJANGO_SETTINGS_MODULE is already set by the running InvenTree pod.
echo "Provisioning sandbox-agent user..."
kubectl exec -n "$INVENTREE_NS" "$POD" -- python3 -c "
import django
django.setup()
from django.contrib.auth.models import User
u, created = User.objects.get_or_create(
    username='sandbox-agent',
    defaults={'email': 'sandbox-agent@allegedly.works'},
)
u.set_password('${SANDBOX_PASSWORD}')
u.save()
print('created' if created else 'updated')
"
echo "sandbox-agent user provisioned."

# Fetch the stable DRF API token via the public REST endpoint.
# DRF tokens are idempotent: the same token is returned on repeated calls.
echo "Fetching API token..."
TOKEN_RESPONSE=$(curl -sf -X POST "$INVENTREE_SVC/api/user/token/" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"sandbox-agent\",\"password\":\"${SANDBOX_PASSWORD}\"}")
TOKEN=$(echo "$TOKEN_RESPONSE" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
  echo "ERROR: Could not extract token. Response: $TOKEN_RESPONSE"
  exit 1
fi
echo "Token obtained (first 8 chars): ${TOKEN:0:8}..."

# Write token to inventree namespace with Reflector annotations.
# Reflector auto-mirrors this Secret to openclaw-sandbox and claude-sandbox.
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: ${SECRET_NAME}
  namespace: ${INVENTREE_NS}
  annotations:
    reflector.v1.k8s.emberstack.com/reflection-allowed: "true"
    reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces: "openclaw-sandbox,claude-sandbox"
    reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "openclaw-sandbox,claude-sandbox"
type: Opaque
stringData:
  token: "${TOKEN}"
  username: "sandbox-agent"
EOF

echo "Provisioning complete. Reflector will mirror to openclaw-sandbox and claude-sandbox."
