# codex-nix-pod spike

Experimental, manually applied pod for testing a Codex-like agent environment as a
normal Kubernetes pod with a writable persistent Nix store.

This is deliberately under `agents/x/` and is not referenced by any Flux
`Kustomization`.

## Apply

```bash
kubectl apply -k cluster/k8s/agents/x/codex-nix-pod
kubectl -n codex-nix-pod wait --for=condition=Ready pod/codex-nix-pod --timeout=10m
```

## Smoke

```bash
kubectl -n codex-nix-pod exec -it codex-nix-pod -- sh
```

Inside the pod:

```bash
mkdir -p ~/.config/direnv
nix shell nixpkgs#nix-direnv -c bash -lc 'echo "source $(dirname "$(realpath "$(which nix-direnv)")")/../share/nix-direnv/direnvrc" > ~/.config/direnv/direnvrc'
```

Then put a checkout under `/workspace/ducktape`, run `direnv allow`, and verify
that `.envrc` realizes the repo devshell onto the persistent `/nix` volume.
