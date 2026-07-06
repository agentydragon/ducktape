# wyrm2 Suspension — 2026-05-10

Machine will be physically relocated and offline for ~2 weeks.

## Suspend + Delete (with data removal)

- [x] **props** — deleted Deployment, CNPG cluster (`props-db`), PVC, all Secrets, namespace. Set `spec.suspend: true` on Flux Kustomizations (props-{namespace,db,secrets,app}, harbor-props).
- [x] **openhands** — deleted Deployment, PVC (`openhands-data`), all Secrets, namespace. Set `spec.suspend: true` on Flux Kustomizations (openhands-{namespace,secrets,sandboxes,app}).
  - `TODO`: reevaluate whether to redeploy after machine comes back.
- [x] **openclaw** — backed up persistent state to `~/drive/2026-05-10-cluster-suspensions/openclaw/openclaw-data.tar.gz` (17Mi). Deleted StatefulSet, PVCs, Deployments, Services, Secrets, ConfigMaps, NetworkPolicies, and all 3 namespaces (openclaw-gateway, openclaw-mitmproxy, openclaw-sandbox). Set `spec.suspend: true` on 8 Flux Kustomizations.
  - `TODO`: play with it again once machine is restored.
- [x] **thrive-scraper** — deleted CronJob, Deployment, PVC (`thrive-data`), all Secrets, namespace. Set `spec.suspend: true` on Flux Kustomization in gaffer-private.

## Down but Not Suspended

- [ ] **ollama** — stays in k8s, just offline until wyrm2 comes back up.

## Suspended (wyrm2 offline, no Proxmox nodes)

- [x] **proxmox-proxy** — suspended in git (`spec.suspend: true`). Re-enable once atlas is back up.
- proxmox-proxy resources (deployment, service) deleted from cluster.
- openebs, nvidia-device-plugin, node-feature-discovery, kube-system/cilium — node-level infra, no action needed

## No Action Needed (will reschedule to VPS automatically)

- **manifold-mcp** — stateless Deployment, will reschedule to VPS
- **cnpg-system** (operator) — will reschedule; single-replica CNPG clusters won't attempt failover (nothing to fail over to)
- **flux-system/tofu-controller** — stateless Deployment, will reschedule
- **google-workspace-mcp** — already broken (Init:CreateContainerConfigError), no action changes nothing
- **loki** (canary + promtail) — loki-0 already on vps-worker-0; DaemonSet instances on wyrm2 just stop
- **monitoring** (grafana, grafana-operator, node-exporter) — most stack already on VPS (mimir, tempo, alloy); grafana/operator will reschedule; node-exporter DaemonSet stops on wyrm2
- **csi-proxmox** — controller Deployment will reschedule; node plugin DaemonSet stops on wyrm2
- **kvm-device-plugin** — DaemonSet, stops when wyrm2 goes down, irrelevant on VPS (no KVM)
- **cpap-sync** — CronJob already paused (now pushes to the `cpap-data` Forgejo repo; the wyrm2-pinned webdav/PVC setup was removed)

## Decide What To Do

### Apps that serve externally (need to decide: leave running on another node? suspend?):

- [ ] **atuin** (server + CNPG `atuin-db-1`) — shell history sync
- [x] **grocy-sf** (grocy + mcp-server) — migrated to VPS (`local-path-hetzner` PVCs, `hil` nodeSelector). Data restored from kubectl cp backups.
- [x] **grocy-vallejo** (grocy + mcp-server) — migrated to VPS (`local-path-hetzner` PVCs, `hil` nodeSelector). Data restored from kubectl cp backups.
- [x] **matrix** (synapse + CNPG `matrix-db-1`) — deleted Deployment, element-web, CNPG cluster, PVC, all Secrets, HTTPRoutes. Removed the Matrix Flux Kustomization CRs while keeping workload manifests on disk for possible revival.
- [ ] **harbor** (full stack: core, db, registry, redis, portal, nginx, jobservice, exporter) — container registry
- [ ] **nix-cache** (attic + CNPG `attic-db-1`) — Nix binary cache

### Infra/system services:

- [ ] **docker-ci** — CI Docker daemon (needs Docker, likely wyrm2-only)

### Also noted:

- [x] **claude-sandbox/aime-gpt20** — `Error` pod deleted.
- [x] **MCP secret access** — TODO filed at `cluster/docs/todo_k8s_mcp_secret_access.md`.
