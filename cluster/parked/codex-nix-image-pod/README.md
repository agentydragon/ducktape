# codex-nix-image-pod spike

Experimental, manually applied pod for testing a Codex-like agent environment as a
normal Kubernetes pod with an image-backed Nix store.

This is the opposite tradeoff from `../codex-nix-pod`: it deliberately does not
mount a PVC over `/nix`. The base image's Nix closure stays visible, so the pod
can start normally. Any extra Nix store paths installed by `direnv` land in the
container writable layer and disappear when the pod is recreated.

`XDG_CACHE_HOME` is set to `/tmp/xdg-cache` on purpose. During the live spike, a
failed restart left Nix's tarball/git cache malformed when it lived under the
persistent home directory. Keeping that cache ephemeral avoids making `/home`
state capable of crashlooping the pod. The tradeoff is that Nix eval/tarball
cache is cold after pod recreation.

The upstream `nixos/nix` image is root-oriented. This pod therefore emits
`restricted` PodSecurity warnings; making this non-root needs a custom image or a
different writable-store design.

This is deliberately under `agents/x/` and is not referenced by any Flux
`Kustomization`.

## Apply

```bash
kubectl apply -k cluster/k8s/agents/x/codex-nix-image-pod
kubectl -n codex-nix-image-pod wait --for=condition=Ready pod/codex-nix-image-pod --timeout=10m
```

## Smoke

```bash
kubectl -n codex-nix-image-pod exec -it codex-nix-image-pod -- /root/.nix-profile/bin/nix --version
kubectl -n codex-nix-image-pod exec -it codex-nix-image-pod -- /root/.nix-profile/bin/nix shell nixpkgs#bash -c bash
```

Inside a shell with `bash`, install `direnv`/`nix-direnv` and try the repo
checkout under `/workspace/ducktape`:

```bash
mkdir -p ~/.config/direnv
nix shell nixpkgs#direnv nixpkgs#nix-direnv nixpkgs#bash nixpkgs#coreutils -c bash -lc 'echo "source $(dirname "$(realpath "$(which nix-direnv)")")/../share/nix-direnv/direnvrc" > ~/.config/direnv/direnvrc'
```
