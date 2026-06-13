# ActivityWatch

Personal activity tracking via [aw-server-rust](https://github.com/ActivityWatch/aw-server-rust).
Accessible at `activitywatch:5600` via Nebula mesh (lighthouse DNS resolves the cert name).
No built-in auth; Nebula mesh membership is the trust boundary.

- **Server**: `aw-server-rust` on Proxmox, SQLite on `local-path-proxmox` (1Gi PVC)
- **Sidecar**: Nebula container joins the mesh (`10.42.0.40`, cert name `activitywatch`)
- **Image**: `ghcr.io/agentydragon/aw-server`, built with Bazel (`@ducktape_activitywatch//:image`) and pushed by the `push-images.yml` GHA matrix
- **Certs**: SOPS secret (`k8s/activitywatch/nebula-certs.sops.yaml`)
- **Read-only proxy**: nginx sidecar on port 5601 (Service `activitywatch-readonly`),
  allows GET + POST `/api/0/query` only. `openclaw-sandbox` and `claude-sandbox` namespaces
  have CiliumNetworkPolicy access to this port.

## Desktop Client Setup

Watchers run locally, heartbeat to cluster via Nebula mesh. Config managed by
Nix home-manager (`nix/home/services/activitywatch.nix`).

1. Ensure Nebula is running on the host (NixOS workers have it via `nebula.nix`)
2. Apply config: `home-manager switch --flake ~/code/ducktape#<hostname>`
3. Start: `aw-qt` (runs `aw-watcher-afk`, `aw-watcher-window`)
4. Verify: `curl http://activitywatch:5600/api/0/info`
