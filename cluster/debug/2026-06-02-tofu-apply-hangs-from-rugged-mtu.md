# Tofu apply hangs from rugged over Cilium/Nebula — Path MTU mismatch

**Date**: 2026-06-02
**Status**: Confirmed root cause. WiFi underlay fixes the apply hangs. Permanent
fix (MSS clamping at `nebula1` egress) not yet applied.
**Triggering work**: OVH node renames (2026-06).

## Symptom (the headline behavior)

`tofu apply` from rugged (NixOS k8s worker, joined via Nebula mesh) against the
in-cluster CNPG state DB (`tofu-state-db-ovh-rw`):

- Apply runs for several minutes, successfully completing all the actual work
  (cert creates/destroys, Talos machine-config applies).
- Then **hangs silently for 10-20 minutes** at state writeback (no log output
  for the entire window).
- Eventually exits with:

  ```text
  Error: Failed to save state
    Error saving state: write tcp 10.244.6.159:48822->10.99.124.57:5432: write: connection timed out
  Error: Failed to persist state to backend
    ... state has been written to "errored.tfstate" ...
  ```

- The lock_info row that was INSERTed at apply start never gets cleared, so the
  next apply sees "Workspace already locked: default" with a fresh-looking
  `Created` timestamp matching the previous attempt's start.

This makes the entire pipeline look like it has cascading lock problems, when
the actual failure is **at the network layer**.

## What we initially suspected (rabbit holes)

1. **Stale PG advisory locks from orphan kubectl-PF-tunneled sessions.** Wrong —
   `kubectl port-forward` tears down its upstream PG session when the client
   side closes. No long-lived orphan sessions.
2. **In-cluster CR-runners holding the workspace lock.** Wrong — CRs use
   different `schema_name` (per `kubectl get terraform`), so their advisory
   locks don't conflict with our local terraform/main. Also the lock owner
   field shows `agentydragon@rugged`, not `runner@<pod-name>`.
3. **Race between force-unlock and the next apply.** Wrong — same hostname/user
   shown in lock owner; the locks are from MY just-finished attempts.
4. **Proxmox-down-causing-refresh-hang.** Real but separate — addressed by
   sinkholing `proxmox_api_host` to `127.0.0.1` (commit `89e933f72`). Refresh
   now fails fast with "connection refused" for proxmox resources. Doesn't
   fix the writeback hang.
5. **Tofu state push hanging in Go internals before connecting.** Wrong-ish —
   state push WAS opening a connection, but the same MTU/network problem
   prevents it from making progress.

## The actual diagnosis

`ss -tnpie` on the hung apply's PG socket while it was in `futex_do_wait`:

```text
ESTAB 10.244.6.159:48822 → 10.244.7.89:5432
  timer:(on, 58sec, 7)         ← TCP retransmit timer ON, 7th backoff
  rto:84736                    ← retransmit timeout 84.7s (started ~660ms)
  bytes_sent:96989
  bytes_retrans:62880          ← 64% retransmissions
  unacked:16  retrans:1/48  lost:13
  lastsnd:26010  lastrcv:591212  lastack:111678
  busy:592602ms                 ← 9.88 min waiting for ACKs
  notsent:13100                 ← 13KB stuck in send queue, cwnd shut
```

The kernel was retransmitting the state writeback for ~10 minutes with no
ACKs. `tcp_retries2=15` (default) means Linux will retry for ~13-30 minutes
before giving up — which matches how long apply hangs before erroring.

### Empirical proof: path MTU is too small for the configured stack

`ping -M do` from rugged to PG pod over Cilium overlay:

| Payload size | Result                   |
| ------------ | ------------------------ |
| 1200 bytes   | ✓ pass                   |
| 1300 bytes   | 100% drop (no ICMP back) |
| 1350 bytes   | 100% drop                |
| 1400 bytes   | 100% drop                |
| 1450 bytes   | 100% drop                |

Layer budget:

| Layer                               | MTU  | Encap overhead       |
| ----------------------------------- | ---- | -------------------- |
| Pod (`cilium_vxlan`)                | 1412 | —                    |
| Cilium VXLAN encap                  | —    | +50 bytes            |
| `nebula1` on rugged                 | 1300 | —                    |
| Nebula encap                        | —    | +38 bytes            |
| Underlying transport                | ???  | (Google Fi cellular) |
| Linux's cached pmtu for this socket | 1362 | (cached, too high)   |

The configured Cilium MTU 1412 implies the path supports VXLAN-encap'd packets
up to 1462 wire bytes. Cilium uses a 1310-byte TCP MSS. A near-MSS segment
produces ~1360-byte VXLAN bytes, which exceeds rugged's `nebula1` 1300 MTU AND
the actual path's ~1200-byte ceiling. **Silent drop, no ICMP Frag-Needed
returned to update pmtu.** Linux retransmits forever.

This is the **same failure class** as
[`2026_02_11_cilium_mtu_cross_node_packet_loss.md`](../docs/lessons_learned/2026_02_11_cilium_mtu_cross_node_packet_loss.md)
(KubeSpan version) — different layers (Nebula vs WireGuard) and different
constraint (Google Fi cellular vs home-to-cloud routing), same root pattern.

### Why apply only fails near the end

Tofu's PG interactions follow a size pattern:

1. **Connect + auth + lock-row INSERT**: small queries, all sub-MSS. Pass.
2. **Provider refresh queries**: small. Pass.
3. **Resource modifications**: small COMMITs. Pass.
4. **State writeback at end**: full state file is ~1.3 MB. Requires
   sustained near-MSS TCP segments. **Fails.**

So every apply does its actual work successfully, then dies on the last step.
The `lock_info` row is INSERTed in (1) but never UPDATEd to NULL at the end.
Each retry leaves a fresh stale-lock row with the new attempt's timestamp,
which is why "Workspace already locked" keeps coming back with `Created`
matching whichever attempt last ran.

### Why Claude Code "backgrounds" applies

Default Bash tool timeout is ~10 min. Tofu's stuck write waits for `tcp_retries2`
to expire (~13-30 min). The harness times out the foreground call long
before tofu errors. The apply is **still alive** at that point, still
retransmitting in the background, occupying state lock. This produced the
illusion that "tofu was killed by the harness and left state dirty."

## State drift discovered as a side effect

`tofu apply -refresh-only` ran during pilot 1 cleanup and reported
"No changes." That was misleading: refresh-only ALSO failed to persist
state (same MTU bug), but its error was masked because there was nothing
visible to apply.

Result: PG state is currently **two pilots behind reality**:

- Pilot 1 (`talos-ks-game-worker-1` → `ovh-ns104963`): applied on the cluster,
  not in PG state.
- Pilot 2 (`talos-kimsufi-cp-0` → `ovh-ns102453`): partial. Cert ops ran on disk
  but the 5 `talos_machine_configuration_apply` targets never executed (the
  cert local-exec for `ovh-ns104963` errored with "refusing to overwrite
  existing cert" because pilot 1 already wrote it).

State of cert files on disk vs PG state:

| Cert                                            | On disk | In PG state  |
| ----------------------------------------------- | ------- | ------------ |
| `talos-ks-game-worker-1.nebula.allegedly.works` | gone    | still listed |
| `talos-kimsufi-cp-0.nebula.allegedly.works`     | gone    | still listed |
| `ovh-ns104963.nebula.allegedly.works`           | exists  | **missing**  |
| `ovh-ns102453.nebula.allegedly.works`           | exists  | **missing**  |

Cluster state itself (ks-game-worker-1 already renamed live, kimsufi-cp-0
still drained-but-running with old hostname) is unaffected by this drift —
the running nodes have their old Nebula certs embedded in Talos secrets,
not consumed from `./nebula-certs/` at runtime.

## Confounding factor: rugged's Nebula trust set may be stale

User callout (worth verifying): when pilot 1 renamed `talos-ks-game-worker-1`
to `ovh-ns104963`, the cert subject changed from
`talos-ks-game-worker-1.nebula.allegedly.works` to
`ovh-ns104963.nebula.allegedly.works`. Rugged's Nebula config (managed by
home-manager) might still reference the old name in `staticHostMap`,
`lighthouse.hosts`, or peer cert pinning. That could degrade rugged's
ability to reach that peer specifically.

Not yet verified. Worth checking with:

```bash
grep -RIn 'talos-ks-game-worker-1\|ovh-ns104963' nix/nixos/modules/nebula.nix
grep -RIn 'talos-ks-game-worker-1\|ovh-ns104963' /etc/nebula/  # on rugged
nebula ... peer-status (or look at journalctl -u nebula)
```

This wouldn't explain the MTU symptoms — those affect the rugged↔kimsufi
path generally — but could be a _secondary_ problem if some specific peer
becomes preferentially unreachable.

## Unblock plan (when network is back to a known-good MTU)

1. **Verify path MTU with `ping -M do` test** above. Need 1462 bytes to pass
   for Cilium's 1412 MTU to be safe.
2. **Delete the conflicting on-disk cert files** so the next apply can
   recreate them cleanly:
   ```bash
   rm cluster/terraform/main/nebula-certs/ovh-ns104963.nebula.allegedly.works.{crt,key}
   rm cluster/terraform/main/nebula-certs/ovh-ns102453.nebula.allegedly.works.{crt,key}
   ```
   (Cert generation is deterministic given same CA+name+ip+groups, so byte-
   identical certs get regenerated.)
3. **Re-run pilot 2 targeted apply.** With healthy MTU, state writeback
   succeeds. The 5 `talos_machine_configuration_apply` targets execute, kimsufi-cp-0
   reboots into `ovh-ns102453`.
4. **Verify** new node Ready, etcd 3/3, delete stale `talos-kimsufi-cp-0` Node
   object, delete orphan PVC (`seaweedfs/mount0-seaweedfs-volume-2`).

## Permanent fixes to consider (separate from immediate unblock)

In order of preference:

1. **Run tofu from inside the cluster.** A pod scheduled on any kimsufi node
   has direct ClusterIP→pod-IP access to PG with zero Nebula hops. The
   existing tofu-controller infrastructure already does this for the
   in-cluster CRs (they all show `Plan no changes` reliably). Migrating
   `terraform/main` to a Terraform CR is non-trivial because of its
   bootstrap-time scope, but a one-shot `kubectl run` with tofu + state
   credentials would unblock all current rugged-as-client work.
2. **TCP MSS clamping at rugged's `nebula1` egress** so MSS gets clamped to
   the Nebula MTU automatically. iptables/nftables rule on `nebula1` with
   `--clamp-mss-to-pmtu`. Fixes ALL TCP flows from rugged through Nebula
   regardless of inner overlay.
3. **Reduce Cilium pod MTU** to fit the worst-case path budget. Currently
   1412; would need ≤ 1162 to be safe over rugged's Google Fi path
   (1300 nebula - 50 vxlan - some slack). Aggressive — affects all
   pod-to-pod traffic cluster-wide. Documented in the 2026-02-11 lesson.
4. **Set `net.ipv4.tcp_mtu_probing=1`** on rugged. Linux-side probe-based
   pmtu discovery; recovers from broken-ICMP networks. Doesn't fix the
   underlying mismatch but lets TCP find the working size organically.

## Resolution: WiFi fixed it (2026-06-02 ~02:30 PDT)

User switched rugged from Google Fi (cellular tether) to home WiFi. Re-ran the
same MTU ladder:

| Payload | Google Fi (before)   | WiFi (after)                   |
| ------- | -------------------- | ------------------------------ |
| 1200    | ✓ pass               | ✓ pass                         |
| 1300    | **100% silent drop** | ✓ pass                         |
| 1400    | 100% silent drop     | `sendmsg: Message too long` \* |
| 1450    | 100% silent drop     | `sendmsg: Message too long` \* |
| 1462    | 100% silent drop     | `sendmsg: Message too long` \* |

\* `Message too long` from the local kernel = **PMTUD works**: kernel knows
the path limit and returns the error to the application immediately. The
silent-drop case on Google Fi was due to the carrier's CGNAT eating ICMP
Frag-Needed, leaving Linux's pmtu cache stuck at 1362 (too high). Per Google
Fi's typical behavior, ICMP is filtered for "security."

Sub-paths:

- WiFi → public internet (`1.1.1.1`): 1472 wire OK, 1492 not OK → clean 1500
  MTU upstream.
- WiFi → Nebula peer over `nebula1` direct (`10.42.0.13`): caps at 1200 wire
  ICMP, kernel returns `Message too long` for 1300 — `nebula1` interface MTU
  1300 is the local ceiling.
- WiFi → Pod IP through Cilium overlay (`10.244.7.89`): 1300 wire OK, 1400
  returns `Message too long` (cilium_host MTU 1412 ceiling). TCP MSS for the
  overlay is 1310 — fits in the Nebula budget once PMTUD adjusts.

Result: `tofu plan` against PG state succeeded with **0.5-2.2% retransmission
rate** (vs 64% on Google Fi). `tofu apply` of pilot 2 (kimsufi-cp-0 →
ovh-ns102453) completed with successful state writeback.

## Permanent fix to apply (not done yet)

Even with WiFi, rugged's Cilium config is fragile — depending on PMTUD ICMP
delivery from intermediate routers. For roaming/cellular reliability, add an
nftables MSS clamp on `nebula1` egress so SYN's MSS is rewritten to fit the
tunnel MTU regardless of ICMP feedback:

```nftables
table inet nebula-mss {
  chain forward {
    type filter hook forward priority 0; policy accept;
    oifname "nebula1" tcp flags syn tcp option maxseg size set rt mtu
  }
  chain output {
    type filter hook output priority 0; policy accept;
    oifname "nebula1" tcp flags syn tcp option maxseg size set rt mtu
  }
}
```

Or simpler equivalent via iptables-nft. This belongs in
`nix/nixos/modules/nebula.nix` (or equivalent home-manager NixOS module).

Also worth setting cluster-wide: `net.ipv4.tcp_mtu_probing=1` (PLPMTUD) via
Talos sysctl patches — falls back to probe-based PMTUD when ICMP is broken.

## Rename project progress

Pilot 1 (`talos-ks-game-worker-1` → `ovh-ns104963`): cluster work succeeded
but state writeback failed silently on Google Fi. Cluster state was correct;
PG state was 1 pilot behind. Discovered via pilot 2's plan showing extra
add/destroy entries from pilot 1's drift.

Pilot 2 (`talos-kimsufi-cp-0` → `ovh-ns102453`): cluster work succeeded but
state writeback failed on Google Fi. Apply attempt also hit cert-overwrite
error from pilot 1's still-on-disk cert files.

**Recovery (2026-06-02 on WiFi):**

1. Restarted `kubectl port-forward` to tofu-state-db-ovh-rw (PF kept dying on
   cellular; on WiFi it's stable).
2. `tofu force-unlock` on the lingering stale lock from the cellular era.
3. `rm` on the two on-disk colliding cert files
   (`ovh-ns104963.nebula.allegedly.works.{crt,key}` +
   `ovh-ns102453.nebula.allegedly.works.{crt,key}`).
4. `tofu apply -auto-approve -lock=false -target=...` succeeded:

   ```text
   Apply complete! Resources: 2 added, 2 changed, 2 destroyed.
   ```

   This single apply caught up BOTH pilot 1's deferred state (creating the
   `ovh-ns104963` cert resource in state) AND pilot 2's work (creating
   `ovh-ns102453` + applying the new Talos machine config to actually rename
   kimsufi-cp-0 → ovh-ns102453).

5. Cluster: `ovh-ns102453` Ready as control-plane within 90s. Etcd quorum
   preserved (3/3 healthy). Deleted stale `talos-kimsufi-cp-0` Node object
   and orphan `seaweedfs/mount0-seaweedfs-volume-2` PVC.

## Known follow-ups (defer)

- Etcd member metadata still reports HOSTNAME `talos-kimsufi-cp-0` for member
  ID `f18500100952cbb4`. This is an etcd-side stale-metadata issue, not
  functional — member identity is by ID, not name. Will refresh on next
  member announce / etcd restart. Worth cleaning up in the rename closeout.
- The `-lock=false` workaround is fine for one-off renames where we
  CONFIRM no other tofu is running. Don't use it in scripted/CI applies.
- Investigate why force-unlock + plan/apply loop kept producing fresh
  lock_info rows. Hypothesis (unverified): each tofu's lock INSERT goes
  through but the COMMIT or subsequent pg_try_advisory_lock fails, leaving
  the row visible to the next attempt. May be specific to MTU-degraded
  connections (small writes succeed, larger ones don't). Worth re-testing
  on WiFi to see if the lock-loop reproduces.
- Does pilot 1's rename of `talos-ks-game-worker-1` → `ovh-ns104963` need a
  follow-up reconfiguration of rugged's Nebula trust roster? Probably yes
  for clean operation; the static_host_map and trusted peer cert subjects
  may reference old names. Check
  `nix/nixos/modules/nebula.nix` or generated config on rugged.

## Evidence preserved

- `/tmp/tofu_apply_cp_1011684.log` — full pilot 2 apply log including
  successful cert ops, then state writeback failure, then cert-overwrite error.
- `cluster/terraform/main/errored.tfstate` — pilot 1's deferred state writeback
  output. **Possibly stale** (could be from pilot 2). Verify timestamp before
  using.
- Multiple `/tmp/tofu_*_$$.log` files for plan/apply attempts during diagnosis.
