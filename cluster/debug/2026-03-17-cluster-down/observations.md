# Cluster Outage: 2026-03-17

## Discovery

- `kubectl` commands timeout connecting to `5.78.106.249:6443`
- `curl -sk https://ollama.allegedly.works/api/version` timeout (exit 28)
- `curl -sk https://litellm.allegedly.works/health` timeout
- Both Hetzner VPS IPs unreachable: `ping 5.78.106.249` and `ping 5.78.43.147` = 100% packet loss
- `curl -sk https://5.78.43.147:6443/healthz` timeout (exit 28)

## Hetzner API Status

Both servers show **running** in Hetzner Cloud API:

| ID        | Name           | Status  | IPv4         |
| --------- | -------------- | ------- | ------------ |
| 123076668 | talos-vps-cp-1 | running | 5.78.43.147  |
| 123076670 | talos-vps-cp-0 | running | 5.78.106.249 |

Servers are 9 days old.

## Proxmox (atlas) Status

- atlas reachable via Tailnet (100.64.0.5), Proxmox API responding (HTTP 401 = auth required)
- wyrm2 reachable via Tailnet (100.64.0.3)

### VM Status on atlas

| VMID  | Name             | Status  |
| ----- | ---------------- | ------- |
| 100   | wyrm             | stopped |
| 104   | win11-template   | stopped |
| 105   | windows11-test   | stopped |
| 110   | wyrm2            | running |
| 1301  | linux-desktop-01 | stopped |
| 9001  | ubuntu-24.04-... | stopped |
| 10000 | talos-pve-cp-0   | running |

## VNC Console Screenshots

### talos-vps-cp-0 (screenshot: talos-vps-cp-0.png)

- Talos v1.12.3, uptime 219h34m (9+ days)
- Kubernetes v1.35.1, controlplane type
- All services healthy: kubelet, apiserver, controller-manager, scheduler
- CONNECTIVITY: OK
- **Visible issues**:
  - OOM controller "victim processes" logs (controller-runtime, runtime.OOMController)
  - KubeSpan peer address overlap warnings
  - Many AVC (SELinux) denied messages for cilium-agent (perf_event, bpf operations)
  - eth0 rename events at 10:42:43Z (interface instability?)

### talos-vps-cp-1 (screenshot: talos-vps-cp-1.png)

- Talos v1.12.3, uptime 87h49m
- Kubernetes v1.35.1, controlplane type
- CPU: 80.8%, RAM: 90.6% (HIGH)
- **Visible issues**:
  - **Massive DNS resolution failures**: "error serving dns request" with "read udp 5.78.43.147:\*->185.12.64.2:53: i/o timeout" - repeated dozens of times
  - AVC denied for postgres (pg_filenode.map access)
  - **Health check failures starting at 10:43:16Z**:
    - `service[cri](Running): Health check failed: rpc error: code = DeadlineExceeded`
    - `service[apid](Running): Health check failed: dial tcp 127.0.0.1:50000: i/o timeout`
    - `service[kubelet](Running): Health check failed: Get "http://127.0.0.1:10248/healthz": context deadline exceeded`
    - `service[containerd](Running): Health check failed: rpc error: code = DeadlineExceeded`
    - `service[machined](Running): Health check failed: dial unix /system/run/machined/machine.sock: i/o timeout`
    - `service[trustd](Running): Health check failed: dial tcp 127.0.0.1:50001: i/o timeout`
  - **All Talos services failing health checks** despite showing as "Running" in summary

### talos-pve-cp-0 (screenshot: talos-pve-cp-0.png)

- Talos v1.12.3, uptime 87h58m
- Kubernetes v1.35.1, controlplane type
- IP: 10.2.1.1/16 (Proxmox VLAN)
- All services healthy in summary bar
- CONNECTIVITY: OK
- **Visible issues**:
  - DNS resolution failures: "read udp 10.2.1.1:\*->8.8.8.8:53: i/o timeout" - many occurrences
  - KubeSpan WireGuard reconfiguration every 30s: "reconfigured wireguard link", "link: kubespan", "peers: 4"
  - Constant kubespan peer reconfiguration suggests mesh instability

## talosctl Evidence (via wyrm2 -> talos-pve-cp-0 at 10.2.1.1)

### Service Status

All services on talos-pve-cp-0 are Running/OK. Notable: etcd last health change was
2h32m ago (around 08:37Z), suggesting some etcd instability earlier.

### etcd

- **3 members**: talos-pve-cp-0 (leader), talos-vps-cp-1, talos-vps-cp-0
- **talos-pve-cp-0 is the leader** (the only reachable member)
- DB size: 423 MB (109 MB in use, 25.77%)
- Raft index: 20226091, term: 114
- **No etcd alarms**
- etcd range requests taking 100-474ms ("apply request took too long") — slow but functional
- grpc transport closing errors present

### KubeSpan Peer Status (from talos-pve-cp-0)

| Peer           | State       | Last Handshake       | Endpoint             |
| -------------- | ----------- | -------------------- | -------------------- |
| talos-vps-cp-0 | **up**      | 2026-03-17T11:08:38Z | 5.78.106.249:51820   |
| talos-vps-cp-1 | **up**      | 2026-03-17T11:08:05Z | 5.78.43.147:51820    |
| wyrm2          | **up**      | 2026-03-17T11:08:37Z | 69.181.145.140:51820 |
| rugged         | **unknown** | never                | 10.215.171.94:51820  |

**Critical finding**: KubeSpan shows both VPS peers as **up** with recent handshakes
(~11:08Z). This means WireGuard mesh between Proxmox and VPS is working — the VPS nodes
ARE reachable via KubeSpan/WireGuard on UDP 51820. Only their public IP TCP/ICMP traffic
is broken.

### kubelet Logs

All recent kubelet messages on talos-pve-cp-0 are:
`"Unable to authenticate the request due to an error: invalid bearer token, service account token has been invalidated"`
— repeated every ~10-15 seconds. This suggests etcd lost quorum at some point and
service account tokens were invalidated/rotated.

### Hetzner Metrics (CPU + Network)

**talos-vps-cp-0**: CPU was 100-400% with active network traffic until ~Mar 16 evening,
then network bandwidth dropped sharply. CPU remained high but network went to near-zero.
Recent slight uptick in network (KubeSpan keepalives?).

**talos-vps-cp-1**: Similar pattern. CPU 170-370%. Network was active until a sharp
drop. Recent burst of outbound traffic (~13M bps) suggests something still running.

### Hetzner Status Page

No active incidents for hil-dc1 (Hillsboro). Last incident was a load balancer outage
on March 11 (resolved). No network issues reported.

## Timeline (all times UTC, 2026-03-17)

### Pre-Outage Background

- Cluster running 9+ days since last bootstrap (talos-vps-cp-0 uptime: 219h)
- rugged (NixOS laptop) running **kubespand** (custom KubeSpan daemon, older version
  with known bugs — NOT actual Talos) as a roaming worker node
- Hetzner metrics show VPS nodes at sustained high CPU (100-400%) with active network
  traffic through Mar 16 evening

### ~Mar 16 Evening: Network Traffic Drop on VPS

- Hetzner metrics: network bandwidth on both VPS nodes drops sharply to near-zero
- CPU remains high on both nodes
- **Root cause of VPS network drop unknown** — VPS dmesg was lost on reboot

### 10:02:59Z: Rugged KubeSpan Endpoint Flapping (observed from talos-pve-cp-0)

- controller-runtime on talos-pve-cp-0 shows rugged's KubeSpan endpoint changing
  every 30 seconds, cycling through 6+ addresses:
  - `172.17.0.1:51820` (Docker bridge — should never be an endpoint)
  - `100.64.0.6:51820` (Headscale IPv4)
  - `[fd7a:115c:a1e0::6]:51820` (Headscale IPv6)
  - `10.244.0.214:51820` (pod CIDR — should never be an endpoint)
  - `[2601:640:c900:17b0:...]:51820` (public IPv6, varying)
- **Cause**: kubespand bug — advertising all local addresses including Docker bridge
  and pod CIDR IPs as KubeSpan endpoints. Each change triggers a full WireGuard link
  reconfiguration on all peers.

### 10:05:01Z: KubeSpan Peer List Temporarily Shrinks

- talos-pve-cp-0 controller-runtime: WireGuard peer list drops from 4 to 2
  (only VPS cp-0 and cp-1 remain; rugged and wyrm2 removed)
- Quickly restored to 4 peers in subsequent reconfiguration

### 10:39:29Z: DNS Failures Begin on talos-pve-cp-0

- First DNS timeout: `read udp 10.2.1.1:52679->8.8.8.8:53: i/o timeout`
- Continuous flood of DNS timeouts from 10:39:29Z through at least 10:42Z
- talos-pve-cp-0 is on a home network behind NAT — upstream DNS (8.8.8.8) requires
  internet access through the home router
- **Possible cause**: home network internet outage, or KubeSpan routing confusion
  directing DNS traffic through the WireGuard tunnel instead of default route

### 10:41:59Z: KubeSpan Peer Address Overlap

- `peer address overlap` between wyrm2 and rugged:
  - Both claim `10.96.0.0/12` (service CIDR)
  - Both claim `172.17.0.1` (Docker bridge IP)
- Triggers nftables chain reconfiguration (`kubespan_outgoing`, `kubespan_prerouting`)
- **Cause**: kubespand on rugged advertising pod/service CIDRs as its own addresses

### ~10:42Z: KubeSpan Peer Status (snapshot from talosctl)

| Peer           | State       | Last Handshake       | Endpoint             |
| -------------- | ----------- | -------------------- | -------------------- |
| talos-vps-cp-0 | **up**      | 2026-03-17T11:08:38Z | 5.78.106.249:51820   |
| talos-vps-cp-1 | **up**      | 2026-03-17T11:08:05Z | 5.78.43.147:51820    |
| wyrm2          | **up**      | 2026-03-17T11:08:37Z | 69.181.145.140:51820 |
| rugged         | **unknown** | never                | 10.215.171.94:51820  |

VPS peers show **up** with recent handshakes via WireGuard (UDP 51820).
Rugged shows **unknown** / never handshake — consistent with endpoint flapping.

### 10:43:16Z: VPS Services Failing Health Checks (from VNC screenshot)

- talos-vps-cp-1: ALL Talos services failing health checks
  (cri, apid, kubelet, containerd, machined, trustd)
- CPU: 80.8%, RAM: 90.6%
- DNS failures: `read udp 5.78.43.147:*->185.12.64.2:53: i/o timeout`

### ~11:00Z: Discovery / Investigation Begins

- `kubectl` from rugged times out connecting to `5.78.106.249:6443`
- Both VPS IPs unreachable via ping from rugged (100% packet loss)
- VPS nodes running per Hetzner API
- talos-pve-cp-0 accessible via SSH through atlas

### 11:21-11:36Z: etcd Severely Degraded

- etcd on talos-pve-cp-0 (leader, only reachable member): range requests taking
  100-568ms (vs 100ms target)
- "leader failed to send out heartbeat on time; exceeded-duration: 338ms"
- "leader is overloaded likely from slow disk"
- No quorum for writes (2/3 members unreachable)
- kubelet: "invalid bearer token, service account token has been invalidated"

### ~11:30Z: Rebooted Both VPS via Hetzner API

- `hcloud server reboot talos-vps-cp-0`
- `hcloud server reboot talos-vps-cp-1`

### Post-Reboot

- VPS nodes boot cleanly, all services healthy
- kubectl accessible from wyrm2 (was accessible the whole time?)
- VPS still unreachable from **rugged** — separate local network issue
- All 5 cluster nodes show Ready

## Recurring Incident (~12:10Z / ~05:10 PDT, minutes after first reboot)

Note: local time is PDT (UTC-7). All timestamps below are UTC unless marked.

Hetzner action log shows the first reboot at 11:12Z. VNC screenshots taken at
~12:08Z show uptimes of 55m (cp-0) and 52m (cp-1) — consistent with 11:12Z reboot.
The Prometheus OOM kill visible in dmesg occurred at 12:01Z, **49 minutes after boot**.

### Symptoms

- kubectl from rugged times out to `5.78.106.249:6443` again
- talosctl to both VPS IPs (port 50000) times out
- Hetzner API: both servers show `status=running`
- **kubectl works from wyrm2** (via KubeSpan through Proxmox CP)

### VNC Screenshots (saved as `vps-cp-0-recurring-oom.png`, `vps-cp-1-recurring-veth-churn.png`)

**talos-vps-cp-0**: uptime 55m25s (was rebooted ~04:05Z), CPU 40.6%, RAM 74.0%

- **OOM kill of Prometheus visible in dmesg**:
  - `oom-kill:constraint=CONSTRAINT_MEMCG` (cgroup memory limit hit)
  - `Killed process 106738 (prometheus) total-vm:8912708kB, anon-rss:2089204kB`
  - Prometheus was using **~2GB RSS** when killed
  - `oom_score_adj:868` — Kubernetes set high OOM score (burstable QoS)

**talos-vps-cp-1**: uptime 52m12s, CPU 12.9%, RAM 54.2%

- Massive `eth0: renamed from tmp*` messages — hundreds of veth interfaces being
  created/destroyed (pod churn from crashlooping pods)
- Log spans 00:13Z through 07:57Z — continuous veth churn for hours

**talos-pve-cp-0**: uptime 89h21m, CPU 17.1%, RAM 62.4% — all services healthy

- KubeSpan WireGuard reconfiguration still happening every 30s (rugged flapping)

### Node Memory Usage (from `kubectl top nodes` via wyrm2)

| Node           | CPU   | CPU% | Memory  | Memory% |
| -------------- | ----- | ---- | ------- | ------- |
| talos-vps-cp-0 | 1545m | 39%  | 6565Mi  | **92%** |
| talos-vps-cp-1 | 1354m | 34%  | 3612Mi  | 50%     |
| talos-pve-cp-0 | 805m  | 20%  | 4678Mi  | 64%     |
| wyrm2          | 4391m | 13%  | 35861Mi | 37%     |

**talos-vps-cp-0 is at 92% RAM** — critically close to another OOM cascade.

### Root Cause: Pod Memory on talos-vps-cp-0

34 pods running on a 7.5 GiB node. Top consumers (from `kubectl top pods`):

| Pod                          | Memory      | Notes                          |
| ---------------------------- | ----------- | ------------------------------ |
| prometheus-monitoring-0      | 1686Mi      | limit=2Gi, OOM-killed 8+ times |
| kube-apiserver               | 689Mi       |                                |
| longhorn instance-manager    | 674Mi       |                                |
| authentik-server             | 553Mi       |                                |
| authentik-worker             | 425Mi       |                                |
| image-automation-ctrl        | 297Mi       |                                |
| cilium                       | 254Mi       |                                |
| authentik-db                 | 243Mi       |                                |
| longhorn-manager             | 193Mi       | 442 restarts                   |
| **Total visible pod memory** | **~5463Mi** |                                |

Remaining for system services (etcd, kubelet, containerd, kernel): ~1102Mi of 7.5 GiB.

**Prometheus is the primary offender**: 1686Mi current usage, 2Gi limit, OOM-killed
8 times since reboot (55 min ago). The OOM kill of Prometheus on a memory-constrained
node cascades: Prometheus restarts, replays WAL (memory spike), gets OOM-killed again.
Each crash-restart cycle creates veth churn visible on cp-1.

**Authentik stack** (server + worker + db = 1221Mi) is the second largest consumer.

**Longhorn** (instance-manager 674Mi + manager 193Mi = 867Mi) is third.

### Prometheus OOM Details

From `kubectl describe`:

- Container `prometheus`: limit 2Gi, 8 restarts, `Last State: OOMKilled`
- WAL replay during restart consumes peak memory: "WAL segment loaded" messages
  visible in OOM termination log
- Container `config-reloader`: 1 restart, error state (reload timeouts when
  Prometheus is OOM-killed)

### Pod Churn on talos-vps-cp-1

Many pods in `Error`/`Completed` state with high restart counts:

- `longhorn-manager`: 490 restarts
- `cilium-operator`: 27 restarts (Error)
- `cnpg-cloudnative-pg`: 25 restarts (Completed)
- `headscale`: 124 restarts (Completed)
- Multiple Longhorn CSI pods in Error

### Connectivity Notes (for future degraded-cluster debugging)

| Method                       | From   | To            | Status    | Notes                                   |
| ---------------------------- | ------ | ------------- | --------- | --------------------------------------- |
| `kubectl`                    | rugged | VPS 6443      | Timeout   | Kubeconfig points to VPS public IP      |
| `talosctl -e <VPS>`          | rugged | VPS 50000     | Timeout   | Talos API port blocked/unresponsive     |
| `talosctl -e 10.2.1.1`       | rugged | pve-cp-0      | Timeout   | Can't reach Proxmox VLAN from rugged    |
| `kubectl`                    | wyrm2  | via KubeSpan  | **Works** | KubeSpan mesh still functional          |
| `talosctl`                   | wyrm2  | pve-cp-0      | Cert err  | Talosconfig creds mismatch              |
| Hetzner API (`/v1/servers`)  | rugged | Hetzner       | **Works** | Shows server status, metrics            |
| VNC screenshots (Hetzner)    | rugged | Hetzner       | **Works** | Best visibility into VPS state          |
| VNC screenshots (Proxmox)    | rugged | atlas via SSH | **Works** | Best visibility into pve-cp-0           |
| SSH to wyrm2                 | rugged | wyrm2         | **Works** | Via Tailnet/Headscale                   |
| `kubectl` (via SSH to wyrm2) | wyrm2  | cluster       | **Works** | **Best path for kubectl during outage** |

**Key takeaway**: When VPS public IPs are unresponsive, kubectl via wyrm2 (through
KubeSpan mesh) is the best path. Hetzner VNC gives console access without network.

## Analysis

### Two Independent Issues

#### Issue 1: VPS Control Plane Resource Exhaustion (the real outage)

- **Root cause identified: Prometheus OOM kill cascade on memory-starved VPS nodes**
- talos-vps-cp-0 runs 34 pods on 7.5 GiB RAM, reaching 92% utilization
- Prometheus (2Gi limit) is the primary consumer at 1686Mi, repeatedly OOM-killed
  (8 times in 55 minutes). Each restart replays WAL, spiking memory → OOM → restart loop
- Authentik stack (1221Mi) and Longhorn (867Mi) are secondary contributors
- On talos-vps-cp-1: massive pod churn (490 longhorn-manager restarts, many Error pods)
  creates hundreds of veth interfaces, consuming CPU for interface setup/teardown
- Combined memory pressure + OOM kills + pod churn → all Talos services fail health
  checks → node becomes unresponsive to API/ICMP
- etcd on talos-pve-cp-0 became lone leader but had no quorum → service account
  tokens invalidated → kubelet auth failures cluster-wide
- **Resolution**: Hetzner API reboot at 11:12Z, but **issue recurred within 49 minutes** —
  Prometheus OOM-killed again at 12:01Z during WAL replay. Rebooting does not fix the
  underlying problem; the node re-enters the OOM cycle on every boot.

#### Issue 2: Rugged KubeSpan Flapping (why rugged couldn't reach the cluster)

- kubespand (custom KubeSpan daemon for NixOS, older version with bugs) was
  advertising all local addresses as KubeSpan endpoints, including:
  - Docker bridge (`172.17.0.1`)
  - Pod CIDR (`10.244.0.214`)
  - Headscale IPs, public IPv6 (multiple)
- This caused 30-second reconfiguration cycles on all peers
- KubeSpan peer status for rugged: "unknown" / never handshake
- Peer address overlap with wyrm2 (both claiming service CIDR + Docker bridge)
- Even after VPS reboot, rugged couldn't reach VPS — kubespand flapping + possibly
  home network routing issues
- **This did NOT cause the VPS outage** — the VPS issues were independent (resource
  exhaustion visible in VNC screenshots, Hetzner metrics show network drop hours earlier)

### Contributing Factor: Home Network DNS

- talos-pve-cp-0 DNS failures (10:39:29Z) to 8.8.8.8 suggest home internet
  connectivity issues concurrent with (but independent of) VPS issues
- Could be a transient home ISP outage, or KubeSpan routing confusion on
  talos-pve-cp-0 sending DNS traffic through the WireGuard tunnel

## Recommendations

### Immediate (fix the recurring OOM cycle)

1. **Move Prometheus off VPS nodes** — pin to talos-pve-cp-0 or wyrm2 where there's
   more RAM. Prometheus at 1686Mi (growing toward 2Gi limit) is 22% of a 7.5GiB VPS
   node's RAM. On pve-cp-0 (7.7 GiB, 64% used) it would have headroom.
2. **Reduce Prometheus retention/scrape targets** — fewer metrics = less WAL = less
   memory on replay. Consider shorter retention or federation.
3. **Increase Prometheus memory limit** — 2Gi is too low for the current scrape volume.
   But this only works if the node has headroom (it doesn't on VPS).

### Short-term (prevent recurrence)

4. **Rebalance pods across nodes** — 34 pods on talos-vps-cp-0 is too many for 7.5GiB.
   Move non-critical workloads (Longhorn UI, image-automation-controller, Tempo) off VPS.
5. **Add PriorityClasses** — ensure etcd/apiserver/DNS survive when memory is tight,
   by evicting lower-priority pods first (already in `docs/plan.md`).
6. **Consider VPS upgrade to CPX41** — more RAM headroom (already in `docs/plan.md`).

### Longer-term

7. **Upgrade kubespand on rugged** — fix endpoint advertisement to exclude Docker
   bridge, pod CIDR, and other internal addresses.
8. **ResourceQuota per namespace** — prevent any single namespace from consuming
   disproportionate node resources.

## Hetzner Server Action Log

| Time (UTC)           | Server         | Action                               |
| -------------------- | -------------- | ------------------------------------ |
| 2026-03-08T07:07Z    | both           | create + start                       |
| 2026-03-13T18:43Z    | talos-vps-cp-1 | reboot                               |
| 2026-03-17T10:42Z    | talos-vps-cp-0 | request_console                      |
| 2026-03-17T10:45Z    | talos-vps-cp-1 | request_console                      |
| 2026-03-17T11:12:08Z | talos-vps-cp-0 | reboot                               |
| 2026-03-17T11:12:28Z | talos-vps-cp-1 | reboot                               |
| 2026-03-17T11:17Z    | talos-vps-cp-0 | request_console                      |
| 2026-03-17T11:18Z    | talos-vps-cp-1 | request_console                      |
| 2026-03-17T12:01Z    | talos-vps-cp-0 | (Prometheus OOM-killed — from dmesg) |
| 2026-03-17T12:08Z    | both           | request_console                      |

## Saved Evidence Files

- `talos-vps-cp-0.png` — VNC screenshot (pre-reboot)
- `talos-vps-cp-1.png` — VNC screenshot (pre-reboot)
- `talos-pve-cp-0.png` — VNC screenshot (Proxmox)
- `talos-vps-cp-0-post-reboot.png` — VNC screenshot (post-reboot)
- `talos-vps-cp-1-post-reboot.png` — VNC screenshot (post-reboot)
- `vps-cp-0-recurring-oom.png` — VNC screenshot showing Prometheus OOM kill in dmesg
- `vps-cp-1-recurring-veth-churn.png` — VNC screenshot showing veth churn
- `pve-cp-0-recurring.png` — VNC screenshot (healthy, KubeSpan reconfiguration)
- `vps-cp-0-pod-memory.txt` — per-pod memory usage on talos-vps-cp-0
- `talos-pve-cp-0-dmesg.txt` — full kernel dmesg (1807 lines, pre-crash — NOT rebooted)
- `talos-vps-cp-0-dmesg.txt` — dmesg (post-reboot only, pre-crash lost)
- `talos-vps-cp-1-dmesg.txt` — dmesg (post-reboot only, pre-crash lost)
- `talos-pve-cp-0-kubelet-logs.txt` — kubelet logs
- `talos-vps-cp-0-kubelet-logs.txt` — kubelet logs (post-reboot)
- `talos-vps-cp-1-kubelet-logs.txt` — kubelet logs (post-reboot)
- `talos-pve-cp-0-etcd-logs.txt` — etcd logs (summary)
- `talos-pve-cp-0-etcd-full.txt` — full etcd logs (2499 lines)
- `talos-pve-cp-0-controller-runtime.txt` — Talos controller-runtime (1281 lines)
- `talos-pve-cp-0-machined.txt` — Talos machined logs (5628 lines)
- `talos-vps-cp-0-controller-runtime.txt` — controller-runtime (post-reboot)
- `talos-vps-cp-1-controller-runtime.txt` — controller-runtime (post-reboot)
- `talos-pve-cp-0-kubespan-peers.yaml` — KubeSpan peer statuses
- `talos-pve-cp-0-members.yaml` — cluster member list
- `talos-pve-cp-0-proc-net-snmp.txt` — network statistics
- `talos-pve-cp-0-addresses.yaml` — network addresses

## Resolution

### Immediate Fix (applied 2026-03-17)

1. **Pinned Prometheus to wyrm2** (96 GiB RAM, 26% used) via `nodeSelector` in
   `cluster/k8s/monitoring-stack/helmrelease.yaml`. Commit `ae4776565`.
2. **Bumped Prometheus memory limit from 2Gi to 6Gi** (temporary, for WAL replay headroom).
3. **Fixed Longhorn PV topology for wyrm2** — wyrm2's NixOS config was missing the
   `node.longhorn.io/create-default-disk=true` label (Talos nodes get it via machine config,
   but kubespand/NixOS nodes need it in `k8s-worker.nix` nodeLabels). Without this label:
   - Longhorn CSI driver didn't register the topology key on wyrm2's CSINode
   - PVs were created with nodeAffinity excluding wyrm2
   - Prometheus pod stuck Pending (PV couldn't bind to wyrm2)
4. **Label applied immediately** via `kubectl label node wyrm2 node.longhorn.io/create-default-disk=true`,
   then Longhorn CSI plugin pod restarted to re-register topology. Permanent fix in
   `nix/nixos/hosts/wyrm2/default.nix`.
5. **Added dedicated Longhorn disk for wyrm2** — `virtio1` 100GB in Terraform
   (`terraform/nixos-dev-env/main.tf`), mounted at `/var/mnt/longhorn` (matching Kyverno
   policy for `region: proxmox` nodes). Pending tofu apply + NixOS rebuild.
6. **Deleted stale PVC/PV** twice (PV nodeAffinity is immutable). After CSINode topology
   fix, new PV correctly includes wyrm2.

### Result

- Prometheus running on wyrm2: 752Mi memory, both containers ready
- VPS nodes freed: talos-vps-cp-0 dropped from 92% to 86%, talos-vps-cp-1 from 50% to 79%
  (other workloads redistributed, but no longer at critical OOM threshold)
- wyrm2 at 26% memory — ample headroom for Prometheus growth

### Remaining Work (see `docs/plan.md`)

- Investigate which scrape targets / metrics drive Prometheus memory growth
- Analyze WAL size vs `retentionSize: 15GB` configuration
- Right-size Prometheus memory limit (currently 6Gi, overkill for 752Mi usage)
- Unpin from wyrm2 once VPS memory pressure is resolved (PriorityClasses, pod rebalancing,
  possible CPX41 upgrade)
- Apply dedicated Longhorn disk to wyrm2 (tofu apply + NixOS rebuild)
- Migrate Longhorn data from `/var/lib/longhorn` (root filesystem) to `/var/mnt/longhorn`
  (dedicated disk)

## Open Questions

- [x] What consumed 90%+ RAM on VPS nodes? → **Prometheus (1686Mi, 2Gi limit, OOM-killed
      8 times) + Authentik stack (1221Mi) + Longhorn (867Mi) on 7.5GiB nodes with 34 pods**
- [x] Were there OOM kills on VPS nodes? → **Yes, Prometheus OOM-killed by cgroup MEMCG
      constraint, visible in dmesg after recurring incident**
- [x] Why couldn't Prometheus schedule on wyrm2? → **Missing `node.longhorn.io/create-default-disk`
      label on NixOS node → CSINode topology excluded wyrm2 → PV nodeAffinity immutable**
- [ ] What caused network bandwidth to drop on VPS nodes on Mar 16 evening?
      Likely related to the same memory pressure — OOM kills disrupt networking.
- [ ] Is the Cilium eBPF/AVC issue on talos-vps-cp-0 a contributing factor?
- [ ] Why is Prometheus consuming so much memory? Check scrape target count, retention,
      and WAL size. WAL replay on restart is the immediate trigger for OOM.
