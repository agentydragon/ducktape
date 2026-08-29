#!/bin/bash
# Automated setup for SSL-intercepting proxy CA certificate in Talos
set -e

echo "=== Talos Proxy CA Certificate Setup ==="
echo

# Step 1: Extract CA certificate from proxy connection
echo "[1/5] Extracting CA certificate from proxy..."
python3 << 'PYEOF'
import subprocess

# Connect through proxy and get certificates
result = subprocess.run([
    'openssl', 's_client', '-connect', 'ghcr.io:443',
    '-proxy', 'localhost:3128', '-showcerts'
], input=b'', capture_output=True, timeout=10)

certs_text = result.stdout.decode('utf-8', errors='ignore')

# Extract all certificates
certs = []
lines = certs_text.split('\n')
current_cert = []
in_cert = False

for line in lines:
    if '-----BEGIN CERTIFICATE-----' in line:
        in_cert = True
        current_cert = [line]
    elif '-----END CERTIFICATE-----' in line:
        current_cert.append(line)
        certs.append('\n'.join(current_cert))
        current_cert = []
        in_cert = False
    elif in_cert:
        current_cert.append(line)

# Last cert is the CA
if len(certs) >= 2:
    with open('/tmp/proxy-ca.pem', 'w') as f:
        f.write(certs[-1])
    print(f"✓ Extracted {len(certs)} certificates, saved CA to /tmp/proxy-ca.pem")
else:
    print(f"✗ Error: Only found {len(certs)} certificate(s)")
    exit(1)
PYEOF

# Step 2: Base64 encode for Talos config
echo "[2/5] Base64 encoding CA certificate..."
CA_B64=$(base64 -w 0 /tmp/proxy-ca.pem)
echo "✓ Encoded certificate (${#CA_B64} characters)"

# Step 3: Update Talos configuration with Python
echo "[3/5] Updating Talos configuration..."
python3 << PYEOF
import yaml
import sys

CA_B64 = """${CA_B64}"""

# Read config
with open('controlplane.yaml', 'r') as f:
    config = yaml.safe_load(f)

# Ensure machine.registries exists
if 'machine' not in config:
    config['machine'] = {}
if 'registries' not in config['machine']:
    config['machine']['registries'] = {}

# Add/update config for ghcr.io
if 'config' not in config['machine']['registries']:
    config['machine']['registries']['config'] = {}

config['machine']['registries']['config']['ghcr.io'] = {
    'tls': {
        'ca': CA_B64
    }
}

# Write back
with open('controlplane.yaml.new', 'w') as f:
    yaml.dump(config, f, default_flow_style=False, sort_keys=False)

print("✓ Updated configuration saved to controlplane.yaml.new")
PYEOF

# Step 4: Backup and replace
echo "[4/5] Backing up original configuration..."
cp controlplane.yaml controlplane.yaml.backup
mv controlplane.yaml.new controlplane.yaml
echo "✓ Backup saved to controlplane.yaml.backup"

# Step 5: Apply configuration
echo "[5/5] Applying updated configuration to VM..."
if ./talosctl apply-config --talosconfig=talosconfig --nodes 127.0.0.1 --file controlplane.yaml --insecure; then
    echo "✓ Configuration applied successfully"
    echo
    echo "=== Setup Complete ==="
    echo "The Talos VM should now trust the SSL-intercepting proxy CA."
    echo "Monitor installation with: tail -f vm-console.log | grep -E 'pull|install|fetch'"
else
    echo "✗ Failed to apply configuration"
    echo "Restoring backup..."
    mv controlplane.yaml.backup controlplane.yaml
    exit 1
fi
