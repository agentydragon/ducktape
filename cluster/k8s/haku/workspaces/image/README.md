# Haku sandbox image

The toolchain the sandbox-provisioning MCP (<../../../../../haku/sandbox_mcp/>) hands out:
an in-cluster **exec target** where an agent runs `bazel run //cli:…` / `bazel test //…`
against a git-synced `haku-state` checkout. Not an agent-loop runtime — the agent loop lives
in the harness and drives this box through `exec_sandbox`.

Two builds of the same image exist right now:

| File          | Built by                                       | Pushed to                            | Status                                               |
| ------------- | ---------------------------------------------- | ------------------------------------ | ---------------------------------------------------- |
| `Dockerfile`  | `.github/workflows/haku-sandbox-image.yml`     | `ducktape-ci/haku-sandbox-image`     | **Live** — what the SandboxTemplate pulls            |
| `default.nix` | `.github/workflows/haku-sandbox-image-nix.yml` | `ducktape-ci/haku-sandbox-image-nix` | Bazel runs; `bazel test //...` blocked on `rules_js` |

Both bake the same `haku-sandbox-setup.sh`, so the per-claim bootstrap cannot drift between
them. The Nix build exists to end the recurring "the image is missing X" bug (`kubectl`,
then a `python3-minimal` with no `json`, then `jq`/`tea`) by making the tool set one
reviewable list that shares a substrate with <../../../../../x/codex_pod_image/default.nix>.

## Probe results (2026-07-26) — Bazel runs; `bazel test //...` does not complete

Bazel now works in the Nix image. `bazel run //cli:validate` returns **1169/1169 files
valid**, the bootstrap runs end to end (both checkouts, egress CA, git identity), and the
Python tests pass. `bazel test //...` still does **not** complete — it dies in the JS
toolchain. Do not cut over yet.

### What it took

Using nixpkgs' Bazel instead of a bazelisk download is the load-bearing change. Bazel's own
extracted helpers (`process-wrapper`, `linux-sandbox`) are FHS binaries that cannot find
`libstdc++` under NixOS glibc, and **all three ways to fix them after the fact are dead
ends**, each measured:

| Attempt                             | Result                                                                              |
| ----------------------------------- | ----------------------------------------------------------------------------------- |
| `LD_LIBRARY_PATH` in the image      | Bazel does not pass it to `process-wrapper`                                         |
| nix-ld + `--host_action_env=NIX_LD` | Doesn't reach it — `rules_python` runs `exec env -` (seen via `--verbose_failures`) |
| `patchelf` the extracted helpers    | `FATAL: corrupt installation: … is missing or modified` — Bazel checksums it        |
| (`/etc/ld.so.cache`)                | nixpkgs glibc reads its cache from inside its own read-only store path              |

nixpkgs patches those helpers at build time, so the problem never arises. Its `bazel_8` is
**8.6.0** under this flake's pin — exactly `.bazelversion`, so no skew. (Verified in the
pod: `nix eval` against the _unpinned_ registry misleadingly reports 8.7.0.)

### The remaining limit, and what it implies

nix-ld is environment-based, so **every ruleset that sanitizes its environment defeats it**,
and each needs its own passthrough:

- `rules_python` actions run `exec env -` → fixed with `--action_env` / `--test_env`
  (without `--test_env=NIX_LD`, all 14 executed tests failed uniformly in ~0.5s, which
  looks like a broken image rather than a missing passthrough).
- `rules_js`'s `js_binary` wrapper scrubs env before exec'ing node → **still broken**; the
  esbuild lifecycle hook dies `[nix-ld] FATAL: panicked … Posix(2)`.

There is no way to enumerate every ruleset that will scrub its environment. That is the
structural argument against env-based nix-ld in a container, and for a **Debian base with a
Nix-built tool closure** ([devinfra/rbe_image/Dockerfile](../../../../../devinfra/rbe_image/Dockerfile)),
where an ordinary glibc makes downloaded binaries work with no environment at all — while
still keeping the tool list as one reviewable Nix attribute set, which was the point of the
rewrite. The nix_rbe_image notes already call that "the primary (working) approach"; this
probe is independent confirmation.

Meanwhile the **Dockerfile image stays live** and carries every user-facing fix (full
`python3`, `jq`, `tea`, the git CA config, the ducktape clone).

### Also required at cutover, in the pod spec

`sandboxtemplate-haku.yaml` sets `command: ["sleep", "infinity"]`, and a Kubernetes
`command:` overrides the image ENTRYPOINT — so `tini` never becomes PID 1 and reaps nothing
(confirmed: PID 1 in the probe was coreutils). Change it to
`["/bin/tini", "--", "sleep", "infinity"]`, or move the sleep to `args:`.

## Cutting over to the Nix image

The risk is entirely **runtime**, so a green CI build proves nothing about it — the
[nix_rbe_image notes](../../../../../x/nix_rbe_image/README.md) explain why. Re-run this
checklist against any candidate image before switching `sandboxtemplate-haku.yaml`.
(Bracketed link, not `<...>`: an autolink containing `_` gets parsed as emphasis and
prettier rewrites the path — it silently turned this into `nix*rbe_image` once already.)

**The whole checklist runs without touching anything live.** It needs no change to the
SandboxTemplate, the warm pool, or the sandbox MCP's config: the template lives in
`haku-sandbox`, the `forgejo-images-creds` pull secret is already reflected there, and the
`haku` identity has pod CRUD in that namespace — so a throwaway Pod from the `-nix` tag
exercises the entire risk. (A `SandboxClaim` adds warm-pool and TTL mechanics, which are
image-independent; the Pod is the part that can fail.)

Get a tag from the `Haku Sandbox Image (Nix)` workflow — it builds and publishes on PRs as
well as `devel`, precisely so this can run pre-merge. (`workflow_dispatch` is not a
substitute: GitHub only dispatches workflows already present on the default branch, so a new
image workflow cannot be dispatched from its own branch.) The pushed ref is in the run's job
summary.

```bash
kubectl -n haku-sandbox run haku-nix-probe --restart=Never \
  --image=git.allegedly.works/ducktape-ci/haku-sandbox-image-nix:<tag> \
  --overrides='{"spec":{"imagePullSecrets":[{"name":"forgejo-images-creds"}]}}' \
  --command -- sleep 3600
```

Then, inside it:

1. `bazel --version` — should print 8.6.0 and match `.bazelversion`.
2. `bazel run //cli:validate` — exercises the cc toolchain and the hermetic CPython.
   Currently passes (1169/1169).
3. `bazel test //...` — target is **25/26**, with only `//ui/e2e:test_e2e` failing (no
   Docker socket, deliberate). Currently blocked earlier than that, in `rules_js`.
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
