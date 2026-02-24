# Authentik Remote Access (RAC) Setup for `linux-desktop-01`

The Helm chart now provisions the RAC provider, endpoint, outpost attachment, and SSH private key automatically. After Helm applies, you only need to make sure the desktop trusts the matching public key and that the authentik application is visible to the right users.

## 1. Preconditions

- `linux-desktop-01` is reachable at `10.0.201.51` and the Ansible playbook `linux-desktop.yaml` has been applied so XRDP/SSH and the XFCE desktop are installed.
- The Helm release `authentik` has been rendered with `linuxDesktopRac.enabled: true` (the repository defaults cover `linux-desktop-01`).

## 2. Sync the RAC SSH public key to the VM

Helm publishes the key that Guacamole will use in a ConfigMap. Copy it onto the host and append it to `authorized_keys` for the login user (`agentydragon`):

```bash
kubectl -n authentik get configmap linux-desktop-rac-ssh-public-key \
  -o jsonpath='{.data.public-key}' \
  > /tmp/linux-desktop-rac.pub

ssh agentydragon@10.0.201.51 \
  'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys' \
  < /tmp/linux-desktop-rac.pub
```

(If you prefer to manage it in Ansible, feed the same key into the `authorized_key` module.)

## 3. Verify authentik objects

Helm’s blueprints should have created everything under **Applications → Providers**:

- Provider `linux-desktop-rac` (protocol SSH).
- Endpoint `linux-desktop-01-rdp` with protocol **SSH**, host `10.0.201.51`, authentication mode `Prompt` (username only), and the built-in `goauthentik.io/providers/rac/ssh-default` mapping.
- Outpost `linux-desktop-rac-outpost` attached to the “Local Kubernetes Cluster” service connection.
- Application `Linux Desktop (SSH)` (slug `linux-desktop-rdp`) in the “Infrastructure” group.

If they are missing, re-run:

```bash
HELM_CACHE_HOME=$PWD/.helm-cache \
HELM_CONFIG_HOME=$PWD/.helm-config \
helmfile -f k8s/helmfile/helmfile.yaml -l name=authentik apply
```

## 4. Assign access

In the authentik UI, open **Applications → Linux Desktop (SSH) → Access** and make sure `agentydragon` (or the appropriate group) has access.

## 5. Test the portal flow

1. Log into the authentik application portal as `agentydragon`.
2. Click the **Linux Desktop (SSH)** tile.
3. A Guacamole session should open to `linux-desktop-01` without prompting for a password, using the private key sealed in the Helm chart.

If the connection fails, double-check that the public key from step 2 is present on the VM and that the outpost pod is running (`kubectl -n authentik get pods -l authentik.outpost=linux-desktop-rac-outpost`).
