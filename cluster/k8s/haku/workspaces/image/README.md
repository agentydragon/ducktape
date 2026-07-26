# Haku sandbox image

The toolchain the sandbox-provisioning MCP (<../../../../../haku/sandbox_mcp/>) hands out:
an in-cluster **exec target** where an agent runs `bazel run //cli:…` / `bazel test //…`
against a git-synced `haku-state` checkout. Not an agent-loop runtime — the agent loop lives
in the harness and drives this box through `exec_sandbox`.

Two builds of the same image exist right now:

| File          | Built by                                       | Pushed to                            | Status                                    |
| ------------- | ---------------------------------------------- | ------------------------------------ | ----------------------------------------- |
| `Dockerfile`  | `.github/workflows/haku-sandbox-image.yml`     | `ducktape-ci/haku-sandbox-image`     | **Live** — what the SandboxTemplate pulls |
| `default.nix` | `.github/workflows/haku-sandbox-image-nix.yml` | `ducktape-ci/haku-sandbox-image-nix` | Builds in CI; **not yet** cut over        |

Both bake the same `haku-sandbox-setup.sh`, so the per-claim bootstrap cannot drift between
them. The Nix build exists to end the recurring "the image is missing X" bug (`kubectl`,
then a `python3-minimal` with no `json`, then `jq`/`tea`) by making the tool set one
reviewable list that shares a substrate with <../../../../../x/codex_pod_image/default.nix>.

## Cutting over to the Nix image

The Nix build's risk is entirely **runtime**, so a green CI build proves nothing about it.
<../../../../../x/nix*rbe_image/README.md> records that NixOS glibc compiles nix-store paths
into its library search path, and binaries \_downloaded at runtime* then cannot find
`libstdc++.so.6`. This image downloads two such binaries by design: the Bazel that bazelisk
fetches (which runs a bundled JDK) and the hermetic CPython that `rules_python` fetches.

`default.nix` bets that the FHS loader symlink plus `LD_LIBRARY_PATH` are enough, on the
grounds that what actually killed the RBE image was Firecracker's goinit never running the
container's `/init` — a constraint a pod we own does not have. Settle that bet before
switching `sandboxtemplate-haku.yaml`.

**The whole checklist runs without touching anything live.** It needs no change to the
SandboxTemplate, the warm pool, or the sandbox MCP's config: the template lives in
`haku-sandbox`, the `forgejo-images-creds` pull secret is already reflected there, and the
`haku` identity has pod CRUD in that namespace — so a throwaway Pod from the `-nix` tag
exercises the entire risk. (A `SandboxClaim` adds warm-pool and TTL mechanics, which are
image-independent; the Pod is the part that can fail.)

```bash
kubectl -n haku-sandbox run haku-nix-probe --restart=Never \
  --image=git.allegedly.works/ducktape-ci/haku-sandbox-image-nix:<tag> \
  --overrides='{"spec":{"imagePullSecrets":[{"name":"forgejo-images-creds"}]}}' \
  --command -- sleep 3600
```

Then, inside it:

1. `bazel version` — proves bazelisk's downloaded Bazel and its bundled JDK start at all.
   This is the step most likely to fail; a `libstdc++.so.6: cannot open shared object file`
   or a missing-loader `no such file or directory` is the predicted failure mode.
2. `bazel run //cli:validate` — proves the hermetic CPython starts and the cc toolchain
   (`local_config_cc` probing for gcc) resolves.
3. `bazel test //...` — expect **25/26**, with only `//ui/e2e:test_e2e` failing (no Docker
   socket, deliberate). Anything else failing is a toolchain difference, not a known gap.
4. `haku-sandbox-setup.sh` end to end — confirm `.netrc`, the git identity, the egress
   truststore, and both the `haku-state` and `ducktape` checkouts land.

Note the probe Pod gets the namespace's egress fence (the cluster-scoped
`haku-sandbox-force-proxy` CCNP selects the namespace, not the workload) but **not** the
SandboxTemplate's env — so export `HAKU_GIT_USERNAME`/`HAKU_GIT_PASSWORD` from
`haku-forgejo-git` before step 4, or it will fail on the `.netrc` heredoc's `:?` guards.
Delete the probe Pod afterwards; the namespace has a 20-pod quota.

If step 2 or 3 fails, the next thing to try is `nix-ld` via pod env (`NIX_LD`,
`NIX_LD_LIBRARY_PATH`) rather than abandoning the approach — those are exactly the vars
Firecracker made unreachable and a pod spec makes trivial. The Dockerfile stays in the
meantime; it is not costing much beyond the occasional missing tool.

Once the checklist passes: delete `Dockerfile`, fold
`haku-sandbox-image-nix.yml` back into `haku-sandbox-image.yml` under the original image
name, and repoint the SandboxTemplate.

## What the bootstrap does

`haku-sandbox-setup.sh` runs once per claim (the MCP's `bootstrap.script` is just a call to
it). In order: the egress CA into a JVM truststore for Bazel's downloader, the `haku` git
identity, `http.sslCAInfo` so plain `git` trusts the bumped proxy, a two-machine `.netrc`
for both Forgejo hostnames, the `haku-state` clone, and a partial `ducktape` clone so the
run's base-sync step has something to diff against.
