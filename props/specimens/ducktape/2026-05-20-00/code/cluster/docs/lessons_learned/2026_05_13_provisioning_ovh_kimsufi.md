# Provisioning an OVH Kimsufi node (lessons from the first one)

**Date**: 2026-05-13
**Status**: Resolved

## Summary

First-time provisioning of an OVH Eco Kimsufi (KS-1) as a Talos worker hit several
non-obvious snags. None individually serious, but they compounded into a long debug
cycle. Notes here so the next OVH node goes smoothly.

## 1. `data.ovh_dedicated_server_boots` returns iPXE shell, not just Debian rescue

The `boot_type = "rescue"` filter returns **both**:

- `191853` — `ipxe-shell` (interactive iPXE bootloader, **no sshd**, never SSH-able)
- `218949` — `rescue12-customer` (Debian 12 rescue, **the one you want**)

The data source returns only IDs, no kernel/description, so the obvious
`tolist(data.ovh_dedicated_server_boots.kimsufi_rescue.result)[0]` picks iPXE shell.
`remote-exec`'s SSH retry then times out for 15 minutes for no good reason.

**Fix**: hardcode the bootIds as TF locals. The Debian rescue ID (218949) appears
to be stable across servers in the same OVH region. Verify with
`GET /1.0/dedicated/server/{name}/boot/{id}` — look for `kernel: "rescue12-customer"`.

## 2. `ovh_dedicated_server` resource needs `/services/*` API permissions

The new framework-based `ovh_dedicated_server` resource auto-copies
`response.iam.displayName` (the auto-generated server name like `ns103656.ip-...`)
into the `display_name` state attribute on every Read. If your HCL doesn't set
`display_name`, the next Update sees state != plan and calls
`PUT /services/{serviceId}` to "clear" it — which needs `/services/*` permission
that **isn't covered by `/dedicated/server/*`**.

**Fix**: when creating the OVH API token, grant these scopes alongside
`/dedicated/server/*`:

- `GET /services`, `GET /services/*`
- `PUT /services/*`

**…but the HCL workaround is still needed when any cancelled server is in TF
state.** With `/services/*` granted, healthy servers' display_name sync works.
But for a server in cancellation state (OVH service marked for non-renewal),
`PUT /services/{id}` hangs ~10 min and then times out — discovered 2026-05-15
during a Kimsufi-worker replacement when the cancelled worker_0 was still in
state. Keep `display_name = each.value.service_name` in `ovh-nodes.tf` (so
state matches config and no PUT is attempted) until all cancelled servers have
been removed from state or have expired.

**Stopgap** (only useful when running with an older, narrower token): set
`TERRAFORM_OVH_RESTORE_BAREMETAL_DISPLAYNAME_BEHAVIOUR=1` in the environment before
`tofu apply`. That skips the iam → display_name copy, so state stays empty and the
spurious update is never triggered. (See the env-var check in
`resource_dedicated_server.go::Read`.)

## 3. OVH endpoint for HIL is `ovh-us`

The OVH provider config needs `endpoint = "ovh-us"` for the US region (api.us.ovhcloud.com).
HIL servers are managed via the US API. Tokens created at `https://api.us.ovhcloud.com/createToken/`
won't authenticate against `ovh-eu`.

## 4. Running tofu from a non-primary machine

The cluster bootstrap historically ran from wyrm2 (admin age key holder). Running
`tofu apply` from a roaming laptop (rugged) exposes things that were hidden by
assumption:

### 4a. Most SOPS files in the repo are admin-only despite `.sops.yaml` listing user keys

`.sops.yaml` rules can drift from the actual file recipients. Files that were
encrypted before user keys were added as recipients still have admin as the only
recipient. **Audit recipe**:

```bash
for f in $(git ls-files '*.sops.*'); do
  echo "=== $f ==="
  jq -r '.sops.age[]?.recipient' "$f" 2>/dev/null
done
```

Files we found admin-only despite broader `.sops.yaml` rules:

- `secrets/nebula/ca.sops.key` (re-keyed 2026-05-13)
- `k8s/tofu-state/db/credentials.sops.yaml`

To re-key without the admin private key, **the data is often recoverable from elsewhere**:

- `secrets/nebula/ca.sops.key` plaintext is cached in TF state under
  `local_sensitive_file.nebula_ca_key.content` (it gets decrypted and written to disk
  by tofu). Extract via `tofu state pull | jq` and re-encrypt with `sops -e -i`.
- `tofu-state-db-credentials` is also in the cluster as a k8s Secret (the live source).
  Fetch via `kubectl -n tofu-state get secret tofu-state-db-credentials`.

### 4b. Nebula per-node certs (in `terraform/main/nebula-certs/`) are not in git

`null_resource.nebula_node_cert` writes per-node `.crt`/`.key` files to a local
directory and the downstream `data.local_file` / `data.local_sensitive_file` reads
them. The state has the resource but the files are wherever it last ran — not on
a fresh machine.

Trying to apply from a fresh machine fails with
`./nebula-certs/<fqdn>.crt: no such file or directory` for every existing node.

**Recovery (without admin key, without re-signing)**: the cert and key are embedded
verbatim in `machine_configuration_input` on every `talos_machine_configuration_apply.*`
state entry (via `nebula_extension_config` in `nebula.tf`). Extract them out with
`tofu state pull | jq | regex` and write back to `nebula-certs/<fqdn>.{crt,key}`.

(The naive alternative — `tofu taint` and let it re-sign — would rotate every node's
cert simultaneously, causing the whole mesh to reconverge.)

### 4c. PG backend access from non-cluster-workers

`tofu init` needs the PG backend at `tofu-state-db-rw.tofu-state`. From a worker
node it's reachable via ClusterIP. From rugged (not a cluster pod network member):

```bash
kubectl port-forward -n tofu-state svc/tofu-state-db-rw 15432:5432
# then
_pg_pass=$(kubectl -n tofu-state get secret tofu-state-db-credentials -o jsonpath='{.data.password}' | base64 -d)
export PG_CONN_STR="postgres://tfstate:${_pg_pass}@localhost:15432/tfstate?sslmode=disable"
```

`cluster/.envrc` already has the fallback logic (port-forward path when ClusterIP not
reachable), it just defaults to SOPS for the password — fine once `tofu-state-db/credentials.sops.yaml`
is re-keyed to user recipients.

## 5. direnv only re-evaluates on directory change

`cluster/.envrc` decrypts `HCLOUD_TOKEN`, `PROXMOX_VE_API_TOKEN` etc. and exports them.
The repo-root `.envrc` (one level up) doesn't. If you `cd` to repo root and then back
into `cluster/`, direnv re-loads each time; but if a single shell command CDs to
`/cluster/terraform/main` and then the next one reads env, the variables may be empty
because the session env was sourced before the CD. Always make sure to be in a
direnv-active directory before invoking `tofu`.

## 6. OVH provider isn't in `MODULE.bazel`'s `tf_repositories` mirror by default

`rules_tf` pre-fetches provider plugins listed in `MODULE.bazel`'s `tf.download.mirror`
so `validate`/`lint` Bazel targets work hermetically. Adding the OVH provider to
`terraform.tf` is not enough — the mirror also needs the entry:

```python
"ovh": "ovh/ovh:2.13.1",
```

Without that, `bb test //cluster/terraform/main:validate` fails with
"provider not found in mirror" — but only for the OVH provider; everything else
mysteriously breaks too because the validate test does a full `tofu init -plugin-dir`
and that init fails in entirety.

## 7. `ovh_dedicated_server_reboot_task.keepers` schema is `list(string)`

Not `map(string)` like `null_resource.triggers`. The error message is helpful:
`Inappropriate value for attribute "keepers": list of string required`.

## 8. OVH IPMI Serial-Over-LAN access (no Java needed)

OVH's web UI offers a Java JNLP applet for the IPMI console; skip it. There's an
SSH-keyed Serial-Over-LAN endpoint that's plain `ssh`:

```bash
# 1. Grant access (one-shot, scoped to your IP + SSH key + TTL minutes)
AK=$(sops -d ../secrets/ovh-credentials.sops.yaml | yq -r '.application_key')
AS=$(sops -d ../secrets/ovh-credentials.sops.yaml | yq -r '.application_secret')
CK=$(sops -d ../secrets/ovh-credentials.sops.yaml | yq -r '.consumer_key')
MY_IP=$(curl -4 -s ifconfig.me)
SSH_PUB=$(cat ~/.ssh/id_ed25519.pub)
SVC=ns103656.ip-147-135-39.us   # OVH service name

BODY=$(jq -nc --arg k "$SSH_PUB" --arg ip "$MY_IP" '{
  sshKey: $k, ttl: 60, type: "serialOverLanSshKey", ipToAllow: $ip
}')
TIME=$(curl -s https://api.us.ovhcloud.com/1.0/auth/time)
URL="https://api.us.ovhcloud.com/1.0/dedicated/server/$SVC/features/ipmi/access"
SIG="\$1\$$(echo -n "$AS+$CK+POST+$URL+$BODY+$TIME" | sha1sum | cut -d' ' -f1)"
curl -s -X POST -H "Content-Type: application/json" \
  -H "X-Ovh-Application: $AK" -H "X-Ovh-Consumer: $CK" \
  -H "X-Ovh-Timestamp: $TIME" -H "X-Ovh-Signature: $SIG" "$URL" -d "$BODY"

# 2. Wait ~5s for the OVH backend task to finish, then GET the gateway address
TIME=$(curl -s https://api.us.ovhcloud.com/1.0/auth/time)
URL="https://api.us.ovhcloud.com/1.0/dedicated/server/$SVC/features/ipmi/access?type=serialOverLanSshKey"
SIG="\$1\$$(echo -n "$AS+$CK+GET+$URL++$TIME" | sha1sum | cut -d' ' -f1)"
curl -s -H "X-Ovh-Application: $AK" -H "X-Ovh-Consumer: $CK" \
  -H "X-Ovh-Timestamp: $TIME" -H "X-Ovh-Signature: $SIG" "$URL"
# → {"expiration":"…","value":"ipmi@8.sol.ipmi.ovh.us"}

# 3. Connect
ssh -i ~/.ssh/id_ed25519 ipmi@8.sol.ipmi.ovh.us
# Exit with `~.` (tilde dot) as usual for OpenSSH escape sequences.
```

Gotchas:

- `POST /access` returns `{"message":"Missing ttl parameter while calling access"}`
  if `ttl` is omitted; the body shape isn't optional.
- `ipToAllow` must be a single IPv4 (no CIDR like `0.0.0.0/0`).
- The grant TTL is in **minutes**, max 240. You can re-issue freely.
- If you see "You have to request access first" on `GET /access?type=serialOverLanURL`,
  that means the access expired — re-issue with POST.

The Talos kernel needs `console=ttyS0,115200n8` in its cmdline to actually
write to this serial console; otherwise the SOL session shows only the BIOS
POST / iPXE / rEFInd / systemd-boot output, nothing from the OS.

## 9. Don't use rEFInd for the EFI boot loader (set `efi_bootloader_path`)

OVH bare metal always boots through their iPXE for microcode delivery. When
the iPXE script reaches "boot to local disk", the per-server attribute
`efiBootloaderPath` decides which `.efi` file iPXE chainloads:

- **Unset**: iPXE first tries hardcoded paths (`\efi\proxmox\grubx64.efi`,
  etc.), then falls back to rEFInd loaded from an OVH HTTP server. rEFInd
  autodetects `\EFI\Linux\Talos-vX.Y.Z.efi` (the Talos UKI), prints
  "Starting Talos-vX.Y.Z.efi"… and silently fails. UKIs need a UKI-aware
  loader (systemd-stub linkage); rEFInd treats it as a generic EFI app and
  the handoff returns control to firmware, which re-PXEs in a loop. There
  is no error message — just an infinite reboot loop.
- **Set to `\efi\boot\bootx64.efi`**: iPXE chainloads systemd-boot directly
  (the Talos metal image drops `systemd-bootx64.efi` at that fallback path).
  systemd-boot is UKI-aware and loads the Talos kernel correctly.

In `ovh_dedicated_server` (TF):

```hcl
resource "ovh_dedicated_server" "kimsufi" {
  service_name        = ...
  efi_bootloader_path = "\\efi\\boot\\bootx64.efi"
  ...
}
```

The provider sends this verbatim via `PUT /dedicated/server/{name}`. Case is
insensitive on the OVH side. This is the same fix Proxmox-on-OVH users hit
when they upgrade and rEFInd starts picking the wrong loader.

## Resources

- OVH TF provider: `~/code/terraform-provider-ovh` (mirror of github.com/ovh/terraform-provider-ovh)
- OVH API explorer: <https://api.us.ovhcloud.com/console-preview/>
- Token creation: <https://api.us.ovhcloud.com/createToken/>
- OVH iPXE / boot orchestration: <https://github.com/ovh/docs/blob/develop/pages/bare_metal_cloud/dedicated_servers/pxe-with-full-private-dedicated/guide.en-us.md>
- Talos bootloader internals (UKI + systemd-boot): <https://docs.siderolabs.com/talos/v1.12/talos-guides/install/bare-metal-platforms/bootloader>
