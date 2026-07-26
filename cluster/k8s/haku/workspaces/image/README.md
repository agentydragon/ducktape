# Haku sandbox image

The toolchain the sandbox-provisioning MCP (<../../../../../haku/sandbox_mcp/>) hands out:
an in-cluster **exec target** where an agent runs `bazel run //cli:…` / `bazel test //…`
against a git-synced `haku-state` checkout. Not an agent-loop runtime — the agent loop lives
in the harness and drives this box through `exec_sandbox`.

Two builds of the same image exist right now:

| File          | Built by                                       | Pushed to                            | Status                                    |
| ------------- | ---------------------------------------------- | ------------------------------------ | ----------------------------------------- |
| `Dockerfile`  | `.github/workflows/haku-sandbox-image.yml`     | `ducktape-ci/haku-sandbox-image`     | **Live** — what the SandboxTemplate pulls |
| `default.nix` | `.github/workflows/haku-sandbox-image-nix.yml` | `ducktape-ci/haku-sandbox-image-nix` | Bazel fix applied, **re-probe pending**   |

Both bake the same `haku-sandbox-setup.sh`, so the per-claim bootstrap cannot drift between
them. The Nix build exists to end the recurring "the image is missing X" bug (`kubectl`,
then a `python3-minimal` with no `json`, then `jq`/`tea`) by making the tool set one
reviewable list that shares a substrate with <../../../../../x/codex_pod_image/default.nix>.

## Probe results (2026-07-26) — Bazel blocked, fix ported, re-probe needed

Ran the checklist against a throwaway Pod. Everything except Bazel worked; **Bazel could not
execute a single action**. A fix is now applied but **not yet re-probed** — do not cut over
until it is.

**The blocker.** bazelisk downloads Bazel and its bundled JDK starts fine
(`Build label: 9.2.0`) — so the FHS loader symlink works and the launcher is healthy. But
`bazel build` then dies:

```text
process-wrapper: error while loading shared libraries:
  libstdc++.so.6: cannot open shared object file
```

This is the nix*rbe_image failure mode, and the "we own the pod spec, so we can set the
compat env" argument does **not** rescue it: Bazel scrubs the environment before spawning its
own extracted helpers, so `process-wrapper` starts with no `LD_LIBRARY_PATH` no matter what
the image sets. Verified directly — the same binary runs \_with* the variable and fails
_without_ it.

The standard env-independent escape is closed too. An `/etc/ld.so.cache` built at image-build
time would be read by the loader regardless of environment, but **nixpkgs glibc reads its
cache from inside its own read-only store path**, not `/etc`:

```text
ldconfig: Can't create temporary cache file
  /nix/store/…-glibc-2.42-67/etc/ld.so.cache~: Permission denied
```

### The fix: port our own NixOS Bazel module

Both of those are closed _as I first tried them_ — but this repo already solved this exact
problem for NixOS hosts, in
[nix/nixos/modules/bazel](../../../../../nix/nixos/modules/bazel/default.nix), which exists
precisely because "dynamically-linked Bazel-downloaded toolchains" don't work on NixOS. It has
three mechanisms, and the third one invalidates my "environment fixes are impossible" claim:

| Module mechanism     | Ports to a `dockerTools` image?                                                     |
| -------------------- | ----------------------------------------------------------------------------------- |
| `nix-ld`             | **Yes** — symlink its stub at `/lib64/ld-linux-x86-64.so.2`, set `NIX_LD*`          |
| `/etc/bazel.bazelrc` | **Yes** — just a file; `--host_action_env=NIX_LD` re-injects through Bazel's scrub  |
| `envfs`              | **No** — FUSE mount needing systemd; substituted static `/usr/bin/env`, `/bin/bash` |

The `--host_action_env` / `--repo_env` lines are the crux: Bazel scrubs its environment _by
default_, but the module explicitly re-injects `NIX_LD` and `NIX_LD_LIBRARY_PATH`, which is
what lets nix-ld's stub resolve `libstdc++` inside actions. An earlier revision of
`default.nix` concluded env-based fixes were impossible — wrong, and the counter-example was
in-tree the whole time.

All three are now applied (or substituted) in `default.nix`. **Not yet runtime-verified** —
re-run the probe below.

**Why not a full-NixOS container**, which would get all three for free (as
`nixosConfigurations.nix-rbe-worker` does)? It can't boot here:
[the managed-agent README](../../../../../haku/runtime/managed_agent/self_hosted/README.md)
records that "booting systemd PID 1 in an unprivileged container can't mount the API
filesystems", which is why that image runs its closure directly instead. The Haku sandbox pod
has the same constraint (baseline PodSecurity, non-root, caps dropped). So the RBE-era reason
for abandoning NixOS containers (Firecracker never running `/init`) has a k8s analogue after
all — different cause, same outcome.

Meanwhile the **Dockerfile image stays live**, and it is in good shape — the apt build carries
all of this round's real fixes (full `python3`, `jq`, `tea`, the git CA config, the ducktape
clone).

### What the probe did confirm

Against the Pod from `devel-20260726004537-2538fe8`, these all worked: the pull
(778 MB, 37s), non-root uid 1000, the full `python3` stdlib (`json`,
`urllib.request`, `http.client`, `shutil`, `difflib`, `dataclasses`, `sqlite3` all import),
`jq`/`tea`/`kubectl`/`gcc`/`keytool`, the FHS loader symlink, the Kyverno-injected egress CA,
and the `haku-state` clone.

Five defects turned up, all Nix-vs-Debian differences the Dockerfile had papered over.
Four are fixed in `default.nix`; the fifth is the blocker:

1. **`bazel` was not on PATH.** nixpkgs installs the binary as `bazelisk`; every haku-state
   caller says `bazel`. Fixed with a shim derivation.
2. **`/usr/local/bin` is not on PATH** in a pure Nix image, so the bootstrap script was not
   runnable by name.
3. **No `/usr/bin/env`**, so the script's `#!/usr/bin/env bash` shebang died `bad
interpreter`. Both fixed by shipping the bootstrap as a `writeShellScriptBin` package on
   `/bin` instead of a file copied to `/usr/local/bin`.
4. **A baked `GIT_SSL_CAINFO` broke all external git.** Pointing it at the public `cacert`
   bundle looked like a harmless default, but that bundle has none of the egress proxy's CA
   and the variable _overrides_ the `http.sslCAInfo` the bootstrap sets — so the ducktape
   clone failed `unable to get local issuer certificate (20)` while the same `git ls-remote`
   succeeded with the variable unset. Both CA vars are now left to the pod.
5. **`process-wrapper` could not find `libstdc++.so.6`** — the blocker above. Now addressed
   by porting `nix/nixos/modules/bazel` (nix-ld stub + `/etc/bazel.bazelrc` re-injection);
   unverified at runtime. An `/etc/ld.so.cache` was tried first and is impossible here
   (nixpkgs glibc keeps its cache inside its read-only store path).

`bazel run //cli:validate` and `bazel test //...` remain unreachable — not for lack of
headroom, but because of the `process-wrapper` blocker above.

**Also required at cutover, in the pod spec rather than the image:**
`sandboxtemplate-haku.yaml` sets `command: ["sleep", "infinity"]`, and a Kubernetes
`command:` overrides the image ENTRYPOINT — so `tini` never becomes PID 1 and reaps nothing
(confirmed: PID 1 in the probe was coreutils). Change it to
`["/bin/tini", "--", "sleep", "infinity"]`, or move the sleep to `args:`.

## Cutting over to the Nix image

The Nix build's risk is entirely **runtime**, so a green CI build proves nothing about it.
The [nix_rbe_image notes](../../../../../x/nix_rbe_image/README.md) record that NixOS glibc
compiles nix-store paths into its library search path, and binaries **downloaded at runtime**
then cannot find `libstdc++.so.6`. This image downloads two such binaries by design: the
Bazel that bazelisk fetches (which runs a bundled JDK) and the hermetic CPython that
`rules_python` fetches. (Bracketed link, not `<...>`: an autolink containing `_` gets parsed
as emphasis and prettier rewrites the path — it silently turned this into `nix*rbe_image`.)

`default.nix` bet that the FHS loader symlink plus `LD_LIBRARY_PATH` would be enough, on the
grounds that what killed the RBE image was Firecracker's goinit never running the container's
`/init` — a constraint a pod we own does not have. **The probe above disproved that bet**;
see the blocker. Re-run this checklist against any replacement image before switching
`sandboxtemplate-haku.yaml`.

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
