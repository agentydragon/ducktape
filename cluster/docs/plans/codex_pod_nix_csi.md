# Codex pod → Nix-CSI, pull-reconciled

Design notes for evolving the manual `codex-nix-*-pod` spikes
(<../../k8s/agents/x/>, PR #2774) into a proper pull-based workflow where a
`git push` to `devel` reconciles a codex pod with the needed Nix closure, with
**no CI step executing build/deploy commands**.

## Goal

`git push devel` → cluster pulls → codex pod runs with the current Nix tool
environment (codex + git + direnv + a growing tool set), à la how Flux already
reconciles everything else. Adding a tool should be a one-line edit to a Nix
`buildEnv`, not an image rebuild.

## Current state (the spikes)

Three manually-applied, non-Flux pods under `agents/x/` (PR #2774):

- `codex-nix-pod` — writable `/nix` on a PVC (root).
- `codex-nix-image-pod` — image-backed `/nix`, no PVC (root).
- `codex-nix-pvc-uid-pod` — non-root UID 1000, disk-backed `emptyDir` `/nix`
  seeded in an init container, SOPS bootstrap identity, sshd-over-`kubectl exec`.
  This is the runtime shape we like, minus the `/nix` seeding cost.

All three hand-roll "get a `/nix` into the pod" and then `nix shell nixpkgs#…`
at runtime. The seeding (~77s cold start, 80Gi `emptyDir` per pod) is the wart.

## Landscape evaluated

| Tool                                             | Role                                                                                                                        | Verdict                                                                                                               |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **nix-csi** (Lillecarl, `v0.4.2`)                | CSI driver mounting a shared node `/nix` into pods; realizes a requested closure on demand (in-cluster build or substitute) | **Chosen** — deletes the seeding problem; per-pod hardlinked store views share inodes/page-cache                      |
| **Nixery** (tazjin)                              | on-demand registry: `nixery.dev/shell/pkg1/pkg2` as the image                                                               | Fallback if nix-csi proves too raw; 80% of the ergonomics with just an image ref                                      |
| **nix2container** / `dockerTools`                | build OCI images from Nix                                                                                                   | Not our shape — we want no image build in the loop                                                                    |
| **kubenix** (hall) / **easykubenix** (Lillecarl) | NixOS-module → k8s manifests                                                                                                | Use only as a **renderer** if needed; not required for one pod                                                        |
| **kluctl**                                       | templated deploy tool w/ diff + discriminator-prune; CLI or its own controller                                              | **Rejected for this** — it's easykubenix's default deployer but would be a second GitOps control plane alongside Flux |

## Chosen architecture

Compose the two in-cluster pieces; **CI executes nothing**:

- **Flux** does what it already does: watch git, apply YAML, roll on change,
  SOPS-decrypt the identity secret.
- **nix-csi** does the Nix build/realize in-cluster (node daemon + builder pods).

The bridge is the volume reference. Reference the env by **`flakeRef`** (or
`nixExpr`), **not** a pre-built store path — that moves the build off CI and into
the cluster:

```yaml
volumes:
  - name: nix
    csi:
      driver: nix.csi.store
      volumeAttributes:
        flakeRef: git+ssh://git@github.com/agentydragon/ducktape?ref=devel#codexEnv
```

The environment is a plain `buildEnv`; adding tools = editing `paths`:

```nix
# cluster/k8s/agents/x/codex-nix-csi/codex-env.nix (sketch)
pkgs.buildEnv {
  name = "codex-env";
  paths = with pkgs; [
    bash coreutils moreutils git openssh
    direnv nix-direnv lix
    ripgrep fd jq nodejs_22
    # codex-cli: nixpkgs pkg or our overlay derivation
  ];
}
```

The pod uses the `ghcr.io/lillecarl/nix-csi/scratch` base image, which sets
`PATH=/nix/var/result/bin`; the container is otherwise empty. Non-root UID 1000,
SOPS identity, and home-on-SeaweedFS carry over from `codex-nix-pvc-uid-pod`
unchanged (they concern _who runs_ and _home_, not `/nix`).

### Why not the pre-built-store-path + Attic-push approach

That works but requires CI (or a human) to `nix copy` the closure to a
substituter before Flux applies, and to re-render/commit the manifest on every
hash change. The `flakeRef` approach keeps the manifest static and does the build
in-cluster. Attic still helps as a **substituter** (cache hits instead of
building from source) but is no longer a hard prerequisite.

## Open problem: the roll trigger

There is **no off-the-shelf "Flux that builds Nix,"** and Flux does not natively
substitute the git SHA into a manifest field. With an unpinned `?ref=devel`, the
closure is "current devel" _whenever the pod is (re)created_ — so the only gap is
what recreates the pod on push. Options, by how codex is actually used:

1. **Ephemeral per-session pods** — create a fresh pod/Job per codex session; it
   pulls current devel at creation. No trigger, no custom code. Likely the right
   model for agent worker pods. **Start here.**
2. **Tiny in-cluster revision-bumper** — a small controller/CronJob (runs _in_
   the cluster, pulls the Flux `GitRepository` revision — not CI) that bumps a
   pod-template annotation when `devel` moves → Deployment rolls. This is the
   "automatic on push" answer for a persistent pod.
3. **Pin the SHA in the manifest** — each push changes the file, Flux rolls it;
   but something must write the SHA, which (absent CI) is option 2 again.

## Validation / unknowns before promotion

- **Talos + privileged driver**: nix-csi's node component is a privileged
  DaemonSet managing the node's `/nix`. Verify it works on the OVH Talos nodes
  and passes PodSecurity.
- **Private repo fetch**: the in-cluster Nix (builders/node) must fetch the
  private ducktape repo — configure a read deploy key or nix `access-tokens`.
- **Attic as substituter**: point nix-csi's substituters at our Attic so the
  `codexEnv` closure is a cache hit; measure cold build-from-source cost on a
  true miss.
- **`codexEnv` builds in-cluster at all** — the core go/no-go.
- **CSIDriver is `Ephemeral`-only** (`attachRequired=false`,
  `volumeLifecycleModes=[Ephemeral]`): `/nix` is an inline ephemeral volume =
  the node's shared store, not per-pod PVC state. Home stays a normal PVC.

## Phased next steps

1. Land the spikes (PR #2774) — done/in review.
2. Install nix-csi driver on the cluster: render its `kubenix` driver module
   (`CSIDriver` + node `DaemonSet` + RBAC) to YAML once, commit under
   `cluster/k8s/`, apply via Flux; substituters → Attic. Keep it scoped to a
   single test node first.
3. Spike **option 1** under `cluster/k8s/agents/x/codex-nix-csi/`: a
   `codex-env.nix` plus an ephemeral pod using the `flakeRef` volume and a
   builder-with-deploy-key. Prove `codexEnv` builds in-cluster on a Talos node
   and lands on `PATH`.
4. Decide persistent-vs-per-session. If persistent, add the option-2
   revision-bumper.
5. Promote out of `agents/x/`: real Flux `Kustomization` with SOPS decryption,
   decide `agent-box-codex-user` reuse vs a minted `codex-pod` identity, decide
   SSH-over-`kubectl exec` vs a real Service/Cilium listener.

## References

- <../../k8s/agents/x/codex-nix-pvc-uid-pod/README.md> — the spike this evolves.
- nix-csi: <https://github.com/Lillecarl/nix-csi> (`demo/pod.nix`, `kubenix/`).
- Announcement: <https://discourse.nixos.org/t/nixifying-kubernetes-with-nix-csi-easykubenix-and-dinix/70899>.
