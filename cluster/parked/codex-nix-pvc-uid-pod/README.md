# codex-nix-pvc-uid-pod spike

Experimental, manually applied pod for testing a Codex-like agent environment as a
normal Kubernetes pod with an ephemeral writable `/nix` store and a non-root main
container.

This variant keeps the base image's `/nix` visible only in the init container.
The init container mounts an `emptyDir` at `/nix-pvc`, seeds it from the image's
`/nix`, and changes ownership to UID/GID `1000`. The main container then mounts
the same `emptyDir` at `/nix` and runs as UID/GID `1000`.

`codex-home` uses `seaweedfs-ovh-ssd`; `/nix` uses disk-backed `emptyDir`. During the
live spike, seeding `/nix` onto SeaweedFS copied only a few MiB after several
minutes. A node-local path seeded the same store in under a minute. `emptyDir`
keeps the fast node-local behavior without creating a PVC that pins future pods
to one node. The tradeoff is that `/nix` is cold after pod deletion,
rescheduling, or eviction.

This is the runtime shape intended for a later small custom image. For now the
live spike uses `docker.io/nixos/nix:latest` to avoid adding image publishing
plumbing before the Kubernetes mechanics are proven.

Because the upstream image has no UID `1000` passwd entry, this spike mounts a
small `codex-user` ConfigMap onto `/etc/passwd` and `/etc/group`. A custom image
should bake that user in instead. The custom image should also provide a stable
non-Nix entrypoint such as `/bin/sleep`; this spike uses `nix shell
nixpkgs#busybox -c busybox sleep 365d` only because the upstream image has no
normal shell outside `/nix`.

This is parked under `cluster/parked/` and is not referenced by any Flux
`Kustomization`.

Startup shell lives in `scripts/` and is mounted through `configMapGenerator`.
Keep non-trivial scripts there rather than embedding them in `pod.yaml`.

## Secret Bootstrap

`codex-bootstrap-identity.sops.yaml` is a SOPS-managed Kubernetes Secret. Flux
decrypts it with the normal cluster SOPS key, then the root init container mounts
it read-only and copies `id_ed25519` into `/home/codex/.ssh/id_ed25519` with
`0600` permissions and UID/GID `1000`.

The mounted key is the existing `agent-box-codex-user` identity. Home Manager can
then use the same second-stage sops-nix contract as the VM:

```nix
sops.age.sshKeyPaths = [ "${config.home.homeDirectory}/.ssh/id_ed25519" ];
```

The main non-root container does not mount the Kubernetes Secret directly; it
only sees the copied user identity in the persistent home directory.

## Live Result

Verified on 2026-06-29 with `/nix` as disk-backed `emptyDir` (the pod requests `20Gi` and
limits `80Gi` of ephemeral storage):

- the main container runs as `uid=1000(codex) gid=1000(codex)`; `/home/codex` and
  `/workspace` are on `seaweedfs-ovh-ssd`;
- cold start took about 77 seconds end to end (the init seed copy of `/nix` about 36);
  `direnv` + `nix-direnv` + `use flake` realized and ran GNU Hello;
- the init container copies the `agent-box-codex-user` identity from
  `codex-bootstrap-identity.sops.yaml` to `/home/codex/.ssh/id_ed25519` as `1000:1000 600`,
  and its public key matches `ssh_keys/agent-box-codex-user.pub`;
- `ssh codex-pod.allegedly.works` (Home Manager's `ProxyCommand`) reaches in-pod `sshd` over
  `kubectl exec`, and remote SSH sessions have `nix` on `PATH`.

## Apply

This directory includes a SOPS-encrypted Kubernetes Secret. Plain
`kubectl apply -k` is only correct if a SOPS-aware reconciler/renderer decrypts
`codex-bootstrap-identity.sops.yaml` first. In the real cluster, that should be
Flux with `decryption.provider: sops`.

For manual testing, apply the non-secret resources and create
`codex-bootstrap-identity` from a locally decrypted key:

```bash
kubectl apply -f cluster/parked/codex-nix-pvc-uid-pod/namespace.yaml
kubectl apply -f cluster/parked/codex-nix-pvc-uid-pod/configmap.yaml
kubectl apply -f cluster/parked/codex-nix-pvc-uid-pod/pvc.yaml

kubectl -n codex-nix-pvc-uid-pod create configmap codex-pod-scripts \
  --from-file=seed-nix.sh=cluster/parked/codex-nix-pvc-uid-pod/scripts/seed-nix.sh \
  --from-file=start-sshd.sh=cluster/parked/codex-nix-pvc-uid-pod/scripts/start-sshd.sh \
  --dry-run=client \
  -o yaml | kubectl apply -f -

# Create/apply codex-bootstrap-identity from the decrypted
# agent-box-codex-user key. Do this with a temp file and remove it after apply.

kubectl apply -f cluster/parked/codex-nix-pvc-uid-pod/pod.yaml
kubectl -n codex-nix-pvc-uid-pod wait --for=condition=Ready pod/codex-nix-pvc-uid-pod --timeout=10m
```

## Smoke

```bash
kubectl -n codex-nix-pvc-uid-pod exec -it codex-nix-pvc-uid-pod -- /nix/var/nix/profiles/default/bin/nix --version
kubectl -n codex-nix-pvc-uid-pod exec -it codex-nix-pvc-uid-pod -- /nix/var/nix/profiles/default/bin/nix shell nixpkgs#bash -c bash
```

Inside a shell with `bash`, install `direnv`/`nix-direnv` and try the repo
checkout under `/workspace/ducktape`:

```bash
mkdir -p ~/.config/direnv
nix shell nixpkgs#direnv nixpkgs#nix-direnv nixpkgs#bash nixpkgs#coreutils -c bash -lc 'echo "source $(nix eval --raw nixpkgs#nix-direnv)/share/nix-direnv/direnvrc" > ~/.config/direnv/direnvrc'
```

## Next Steps

The next useful step is a Home Manager activation spike:

- add a `nix/home/hosts/codex-pod.nix` host that imports most of
  `cluster/parked/agent-box/nix/home/codex.nix`;
- run `home-manager switch` or the generated activation package from
  `scripts/start-sshd.sh` before starting `sshd`;
- confirm whether Home Manager sops-nix can install user secrets without a user
  systemd manager in this pod shape;
- verify BuildBuddy, attic, and Codex config files land in the expected paths;
- provision fresh Forgejo or Kubernetes identities separately if those integrations
  are needed, since the retired agent-box credentials cannot be reused.

After that, turn the runtime into a small custom image:

- bake in `codex`, `nix`, `bash`, `coreutils`, `busybox`, `openssh`, `sops`,
  `ssh-to-age`, `home-manager`, `direnv`, and `nix-direnv`;
- bake in the `codex` passwd/group entry instead of mounting `/etc/passwd`;
- make the container command a direct script path rather than `nix shell ...`;
- keep `/nix` as disk-backed `emptyDir` unless cold-start cost becomes painful.

Promotion to real GitOps should wait until the Home Manager activation is proven:

- move this out of `cluster/parked/`;
- add a Flux `Kustomization` with SOPS decryption;
- decide whether to reuse `agent-box-codex-user` or mint a new
  `codex-pod` identity;
- decide whether SSH stays private over `kubectl exec` or gets a real Service
  and Cilium listener.
