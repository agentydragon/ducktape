# VPS CP Nebula cert corruption — etcd partition during worker decommission

**Date**: 2026-05-24
**Severity**: Cluster-wide API degradation (~45 min)
**Status**: Resolved

## Summary

A routine `talos-vps-worker-0` decommission via `tofu apply` triggered a Talos
machine-config refresh on `talos-vps-cp-0` and `talos-vps-cp-1` (because removing
the worker changed the `nebula_static_host_map`). The refresh embedded the local
per-node Nebula cert/key files into the new `ExtensionServiceConfig` document
and pushed it to both CPs. Those local files had been silently poisoned 12 days
earlier by a buggy cert-recovery script and contained the CA cert (with leading
indent) where the per-node `host.crt` should have been. Nebula refused to load,
the etcd mesh lost peer reachability across the VPS↔Kimsufi boundary, and the
kube-apiserver lost its etcd leader.

Recovery: regenerated fresh per-node Nebula keypairs locally, hand-built a
multi-doc machine-config (running `v1alpha1` + `HostnameConfig` unchanged, only
the nebula `ExtensionServiceConfig` patched), and pushed it via
`talosctl apply-config --mode=try` then `--mode=auto` to both CPs. Nebula came
back up, etcd quorum reconvened, k8s API recovered.

## Timeline (PT)

- **2026-04-25 02:15** — First Talos config applied to vps-cp-0/cp-1. Embedded
  Nebula host.crt/host.key contained the correct per-node certs (whatever
  `nebula-cert sign` had produced into `cluster/terraform/main/nebula-certs/`
  at that point). Cluster runs normally for ~30 days.
- **2026-05-13 16:01** — Re-keying of `secrets/nebula/ca.sops.key` (admin-only).
  The first OVH Kimsufi worker is added; `local_file.nebula_ca_crt` and
  `null_resource.nebula_node_cert["talos-kimsufi-worker-0..."]` are written.
- **2026-05-13 16:15** — From a fresh machine (`rugged`), `tofu apply` needs the
  `cluster/terraform/main/nebula-certs/*.{crt,key}` files locally. An ad-hoc
  Python script `/tmp/claude/extract_nebula.py` is written to extract them from
  the embedded `machine_configuration_input` in TF state. **Two bugs in the
  script silently produce poisoned files** (details below). The 5 poisoned
  files: `talos-{vps-cp-0,vps-cp-1,pve-cp-0,vps-worker-0,vps-worker-1}.crt|key`.
  Documented in
  [`2026_05_13_provisioning_ovh_kimsufi.md`](2026_05_13_provisioning_ovh_kimsufi.md) §4b.
- **2026-05-13 → 2026-05-24** — Cluster runs fine. Talos config version 1 on
  each VPS CP still has the correct certs from 04-25; nothing triggers a config
  re-push for the VPS CPs, so the poisoned local files are never consulted.
- **2026-05-24 18:42** — `tofu apply` to destroy `hcloud_server.vps["vps_worker0"]`
  (worker decommission). The apply also recomputes
  `talos_machine_configuration_apply.vps["vps0"]` and `["vps1"]` because the
  `nebula_static_host_map` lost the `10.42.0.11` entry → embedded
  ExtensionServiceConfig changes → new Talos config v2 pushed to both VPS CPs.
- **2026-05-24 18:42** — Nebula on both VPS CPs starts crash-looping with
  `unmarshaling pki.key /usr/local/etc/nebula/host.key: input did not contain a
valid PEM encoded block`. Mesh loses VPS↔Kimsufi connectivity. etcd peers
  unreachable. `kubectl` starts returning `etcdserver: no leader`.
- **2026-05-24 19:00** — Diagnosis: `talosctl get extensionserviceconfig nebula
-o yaml` on vps-cp-0 shows `host.crt`/`host.key` with 8-space leading indent
  on every PEM body line. Local files at
  `cluster/terraform/main/nebula-certs/talos-vps-cp-*.crt` are identical to
  `ca.crt` (decoded as `isCa=true name="allegedly.works"`) with 8-space indent.
- **2026-05-24 19:35** — `nebula-cert sign` produces fresh keypairs locally
  for cp-0/cp-1 against the unchanged CA. Hand-built multi-doc Talos config
  splices the running v1alpha1+HostnameConfig docs byte-for-byte with a
  patched ExtensionServiceConfig (only `host.crt` and `host.key` differ).
  `talosctl apply-config --dry-run` confirms only the cert/key change.
- **2026-05-24 19:38** — Apply on cp-0 with `--mode=try` (120s rollback).
  `ext-nebula` returns to `Running`, etcd `HEALTH OK` 56s later. Committed
  with `--mode=auto`. cp-1 applied directly with `--mode=auto`.
- **2026-05-24 19:42** — `etcd members` shows 3-member quorum (cp-0, cp-1,
  kimsufi-cp-0), `kubectl get nodes` works, recovery complete.

## Root cause

The local cert-extraction script `/tmp/claude/extract_nebula.py` (written
2026-05-13 16:15 PT) had two compounding bugs:

````python
# Bug 1: non-greedy regex matches the FIRST PEM CERTIFICATE block in each
# node's machine_configuration_input. But the embedded ExtensionServiceConfig
# lists files in order [ca.crt, host.crt, host.key, config.yml] — so the first
# CERTIFICATE block is the CA cert, not host.crt. Every node's "host.crt" file
# was written with the CA cert content.
crt_m = re.search(
    r"-----BEGIN NEBULA CERTIFICATE V2-----.*?-----END NEBULA CERTIFICATE V2-----\n",
    mci,
    re.DOTALL,
)

# Bug 2: dedent uses min(indents). The regex captures from the literal
# `-----BEGIN ...` character, so leading whitespace before BEGIN is excluded.
# But subsequent body lines retain their 8-space YAML block-scalar indent.
# Result: indents = [0, 8, 8, 8], min = 0, dedent removes 0 chars.
def dedent_pem(text: str) -> str:
    lines = text.splitlines()
    indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    i = min(indents) if indents else 0
    return "\n".join(l[i:] if len(l) >= i else l for l in lines) + "\n"
```text

Net result per node `.crt`:

```text
-----BEGIN NEBULA CERTIFICATE V2-----
        <CA cert base64 line 1>
        <CA cert base64 line 2>
        <CA cert base64 line 3>
        -----END NEBULA CERTIFICATE V2-----
```text

The `.key` files were correctly extracted as per-node X25519 keys (only one
PRIVATE KEY block per node), but inherited the same indent bug, so Nebula
parsed them as malformed PEM.

## Why the cluster ran fine for 12 days

Talos persists the applied machine config (the multi-doc YAML) in the
`MachineConfig.config.persistent` resource. It is consulted **at apply time
only** — the embedded cert/key bytes are mounted into the nebula extension
service via `ExtensionServiceConfig.configFiles`. So Talos was happily using
the original good certs from 04-25's apply; the poisoned local files only
mattered the next time `talos_machine_configuration_apply.vps[*]` ran.

That trigger came 12 days later when the worker decommission caused
`nebula_static_host_map` to lose its `10.42.0.11` entry, which propagated
through `local.nebula_extension_config["vps0"]` →
`data.talos_machine_configuration.vps_nebula["vps0"]` →
`talos_machine_configuration_apply.vps["vps0"]`. Same path for cp-1.
Same apply re-reads the local cert files via the
`data.local_file.nebula_node_crt` / `data.local_sensitive_file.nebula_node_key`
data sources, which by 05-24 were the corrupted versions.

## Why `null_resource` didn't notice the corruption

`null_resource.nebula_node_cert` writes per-node `.crt`/`.key` files via
`local-exec`. Its `triggers` block only depends on `ca_hash`, `ip`, and
`groups`. None of those change when a local file is _modified or deleted by an
outside process_. Tofu has no way to know the on-disk file content drifted —
state thinks "cert was generated", local file might be anything.

This is a general gotcha with `null_resource` + `local-exec` writing
material to disk: state is decoupled from output integrity. Tofu won't
re-run the local-exec until a trigger changes, regardless of what happens
to the output files between runs.

## Recovery procedure (record for next time)

1. Generate fresh per-node Nebula keypairs locally with `nebula-cert sign`
   against the unchanged CA in `nebula-certs/{ca.crt,ca.key}`. Other nodes
   in the mesh need no changes because they verify peers via the CA cert,
   not per-node certs.

2. For each affected node, build a multi-doc YAML to send via
   `talosctl apply-config -f <file>`. **Caveat**: `apply-config` requires a
   v1alpha1 main config doc; submitting only an `ExtensionServiceConfig`
   fails with `the applied machine configuration doesn't contain v1alpha1
config, did you mean to patch the machine config instead?`. The patch CLI
   (`talosctl patch`) doesn't support `ExtensionServiceConfig` type either:
   `unsupported resource type: ExtensionServiceConfigs.runtime.talos.dev`.

3. So the workable path is:
   - `talosctl get mc v1alpha1 -o yaml > running-mc.yaml` to fetch the running
     full multi-doc config (stored verbatim in `spec`, as a YAML literal-scalar
     with 4-space indent).
   - Split on `\n---\n`, replace only the trailing nebula
     `ExtensionServiceConfig` doc with a patched copy (host.crt + host.key
     content swapped for fresh PEM; ca.crt and config.yml preserved
     byte-for-byte), splice back together.
   - `talosctl apply-config --dry-run -f patched-mc.yaml --mode=auto` to verify
     the diff is only host.crt + host.key.
   - `talosctl apply-config -f patched-mc.yaml --mode=try --timeout=120s` for
     auto-rollback on first node, watch `ext-nebula` come up healthy, watch
     `etcd` health flip back to OK, then re-apply with `--mode=auto` to commit
     before the rollback timer expires. Apply subsequent nodes with
     `--mode=auto` directly.

4. After the live fix, copy the freshly-generated cert files into
   `cluster/terraform/main/nebula-certs/` so the next `tofu apply` reads the
   correct values rather than re-pushing the broken ones.

5. Push the local `errored.tfstate` back to the PG backend
   (`tofu state push errored.tfstate`) and `tofu force-unlock <id>` to release
   the stuck lock from the partial apply (port-forward dropped mid-apply).

## Apply-pattern critique

The `tofu apply -refresh=false -auto-approve` invocation I used to destroy the
worker was wrong on two axes:

- **`-refresh=false`** was used as a workaround for the Proxmox provider
  hanging on the unreachable Proxmox API. It suppressed drift detection on
  non-targeted resources, AND it suppressed visibility into what the targeted
  apply was _actually_ about to change. The `Plan: 0 to add, 2 to change, 4
to destroy` summary buried the two `talos_machine_configuration_apply.vps[*]`
  updates inside an output that I had piped through `tail -150`, which trimmed
  those two diff blocks from view.

- **`-auto-approve`** removed the human-in-the-loop review on shared
  infrastructure changes. Even with `-target=`, an in-place update on a
  control-plane node's full machine config is not a routine action.

The right pattern for in-place changes to shared TF state, especially when
external API access is partial:

- Always run `tofu plan -out=plan.bin` first; review the full plan (do not
  `tail` it).
- Apply with `tofu apply plan.bin` (uses the saved plan, no re-plan).
- For partial-network situations, use `-target=<specific resource instance>`
  rather than `-target=<resource collection>` so the blast radius is exactly
  what you reviewed.
- Never combine `-refresh=false` with `-auto-approve` on shared infra. If
  refresh is broken, fix refresh first (or restrict the apply to resources
  that don't need the broken provider).
- If TF state lives behind a kubectl port-forward, keep the port-forward in a
  foreground terminal so a drop is immediately visible (the failed apply
  caused `errored.tfstate` because the port-forward died mid-write).

## Lessons / follow-ups

1. **Cert-extraction recovery script needs replacing.** The
   `tofu state pull | jq | regex` recipe in
   [`2026_05_13_provisioning_ovh_kimsufi.md`](2026_05_13_provisioning_ovh_kimsufi.md) §4b
   produced this incident. The doc has been updated with a warning and a
   tested replacement that uses a YAML parser (no regex) and iterates the
   `configFiles` array to find `host.crt` by `mountPath`, not by position.

2. **Pre-apply file integrity check** for `null_resource.nebula_node_cert`.
   Adding a `triggers.file_hash` that depends on the
   `data.local_file.nebula_node_crt[name].content` _for the matching node_
   would cause Tofu to detect on-disk corruption and re-run the local-exec.
   Tradeoff: the data source would need to be split per-node and the trigger
   structure rebuilt. Worth doing.

3. **`local-exec` writing secrets-to-disk is fragile.** A safer pattern is to
   store the certs encrypted (SOPS or in-state-only) and bind them to the
   `ExtensionServiceConfig` from in-memory rather than via disk-mediated data
   sources. Larger refactor; tracked separately.

4. **Lock the on-disk cert dir to a known checksum.** A small pre-commit /
   pre-apply check could verify each `nebula-certs/<fqdn>.crt` decodes as a
   non-CA cert whose name matches the file's FQDN. The poisoned files would
   have failed that check on day one.

## State of the world after recovery

- `hcloud_server.vps["vps_worker0"]` destroyed (Hetzner server gone, TF state
  consistent post-`state push`).
- `talos-vps-worker-0` k8s Node still present in `kubectl get nodes`
  (cordoned, kubelet gone, will go `NotReady` and can be `kubectl delete
node`'d).
- `cluster/terraform/main/nebula-certs/`: all 6 active node cert files (vps-cp-0,
  vps-cp-1, pve-cp-0, kimsufi-cp-0, kimsufi-worker-0, kimsufi-worker-1) verified
  proper PEM. Orphan `talos-vps-worker-{0,1}.{crt,key}` files deleted.
- Documentation: `cluster/README.md` node table updated, decommission plan
  `plans/decommission-vps-workers.md` deleted, TF removed `vps_worker0` from
  `local.vps_nodes` / `nebula_node_names` / `nebula_static_host_map` /
  `nebula_configs` / `talos_nebula_nodes`.
- `hcloud-csi` controller updated to tolerate the control-plane taint
  (commit `17c456803`) so it can land on a CP after the worker is gone.
- Outstanding: `tf/gitops/dns-records/main.tf` edit (drop `5.78.106.249`
  from `public_gateway_ips`) is staged but not yet applied — separate TF
  root, run when convenient.
````
