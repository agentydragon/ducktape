# sops-nix: container activation without systemd

## Problem

The BuildBuddy API key is decrypted and templated into a bazelrc in **three
independent implementations**:

| Consumer                                                                       | Decryption                                          | Templating                                           | Key source                                         |
| ------------------------------------------------------------------------------ | --------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------- |
| home-manager (`nix/home/modules/buildbuddy.nix`)                               | sops-nix `sops.secrets`                             | sops-nix `sops.templates` (placeholder substitution) | `~/.ssh/id_ed25519` (age via SSH)                  |
| Session start hook (`devinfra/claude/hook_daemon/session_start/buildbuddy.py`) | `_common.sh` → `sops -d` via `SOPS_AGE_KEY` env var | Python f-string in `setup_buildbuddy()`              | `SOPS_AGE_KEY` env var (set in Claude Code web UI) |
| CI / setup script (`devinfra/setup_buildbuddy.sh`)                             | `_common.sh` → `sops -d`                            | bash heredoc                                         | `SOPS_AGE_KEY` from GHA secret                     |

All three produce the same output:

```
common --remote_header=x-buildbuddy-api-key=<decrypted key>
build --config=rbe
```

Goal: define "decrypt this secret and produce this config file" once, and reuse
the same definition in home-manager and in the Claude Code web container (no
systemd, no init).

### Kubeconfig (claude-sandbox)

There are **two distinct kubeconfigs**:

1. **Cluster admin kubeconfig** (`secrets/shared/kubeconfig.yaml`) — full admin
   access via client cert+key. Used only on local machines via home-manager
   sops-nix (`nix/home/modules/kubeconfig.nix`), decrypted verbatim to
   `~/.kube/config`. Not relevant to dedup.

2. **claude-sandbox service account kubeconfig** (`secrets/claude-web-k8s-jwt.yaml`)
   — scoped SA token. Used in **both** environments:

| Consumer                        | Environment | Invocation                                    | Key source                                                       |
| ------------------------------- | ----------- | --------------------------------------------- | ---------------------------------------------------------------- |
| `claude-sandbox-kubectl-mcp.sh` | CLI (local) | `claude-hook write-kubeconfig $TMPKC`         | `SOPS_AGE_KEY` derived from `~/.ssh/id_ed25519` via `ssh-to-age` |
| `claude-sandbox-kubectl-mcp.sh` | Web         | Same script                                   | `SOPS_AGE_KEY` env var from Claude Code UI                       |
| `web_env.sh`                    | Web         | `claude-hook write-kubeconfig ~/.kube/config` | `SOPS_AGE_KEY` env var                                           |

All three go through `write_kubeconfig_cli.py` → `_build_kubeconfig()`, so the
generation logic is **already deduplicated** in Python. The complexity is in
runtime parameters the builder injects:

- `proxy-url` — from `get_proxy_url()`, only set on web (egress proxy)
- `certificate-authority-data` — from session CA bundle or system CAs
  (web needs the MITM CA; CLI doesn't)
- `server`, `service_account`, `namespace` — from the profile's `k8s:` block
  (same in both profiles today)

This is runtime logic that **can't** be a static sops-nix template — the CA
bundle and proxy URL are determined at session start time, not at Nix eval time.

However, the _decryption_ step (`sops -d --extract` of the SA token) is the
same pattern as BuildBuddy. If we had a shared "decrypt secret X from sops file
Y" primitive, `write_kubeconfig_cli.py` could consume the decrypted token from
a known path instead of calling `sops` itself.

### Dedup candidates

Two categories:

**Simple decrypt+template** (same output everywhere, dedup straightforward):

- **BuildBuddy API key** (3 implementations, all produce the same bazelrc)
- Potentially `attic_token`, `HF_TOKEN`, `ANTHROPIC_API_KEY` if the web
  container ever needs them

**Decrypt only** (consumer needs the raw secret, applies its own runtime logic):

- **claude-sandbox k8s token** — already deduplicated at the generation layer
  (`write_kubeconfig_cli.py`), but the decryption step (`sops -d`) could be
  shared with a "materialize decrypted secrets to known paths" primitive
- **Docker mTLS client key** (currently disabled, commented out in
  `_common.sh`). When enabled: `secrets/docker-ci/client-key.sops.pem` is
  decrypted, base64-encoded, and exported as `DUCKTAPE_DOCKER_CLIENT_KEY`.
  The `docker_mtls` pytest fixture (`util/testing/docker_mtls.py`) decodes it
  at test time and assembles a cert dir with the key + public certs from
  runfiles (`ca.pem`, `client-cert.pem`). This is another "decrypt to known
  path/env var" case — the secret is a raw PEM, not a templated config file.
  Would benefit from a shared decryption primitive once docker-ci goes live

## How sops-nix activation works

Nix eval produces a **manifest.json** in the store containing:

- `secrets[]` — encrypted `sopsFile` (store path), YAML `key`, target `path`, `mode`
- `templates[]` — `content` with placeholder markers, target `path`, `mode`
- `placeholderBySecretName` — maps secret names → `<SOPS:hash:PLACEHOLDER>` strings
- `ageSshKeyPaths` / `ageKeyFile` — where to find the decryption key at runtime

The activation is a one-liner:

```bash
sops-install-secrets -ignore-passwd /nix/store/...-manifest.json
```

The systemd user service (`sops-nix.service`, `Type=oneshot`,
`WantedBy=default.target`) just runs this. No systemd dependency in the binary
itself.

### What's NOT exposed

The manifest path and activation script derivation are **internal** to the
sops-nix module — not accessible as `config.*` attributes. The only way to
reach them is indirectly via
`config.systemd.user.services.sops-nix.Service.ExecStart`.

`config.sops.package` gives you the `sops-install-secrets` binary.

## Options

### Option A: Extract activation script from HM eval

Do a standalone home-manager evaluation for the container, import the shared
secret/template module, and fish the activation script out of the systemd
service:

```nix
let
  hmConfig = home-manager.lib.homeManagerConfiguration {
    modules = [
      sops-nix.homeManagerModules.sops
      ./shared/sops-secrets.nix
      { sops.age.keyFile = "/run/secrets/age-key"; }
    ];
  };
  activationScript = hmConfig.config.systemd.user.services.sops-nix.Service.ExecStart;
in
  # activationScript's closure includes sops-install-secrets + manifest + encrypted sops files
```

Run at container entrypoint before the main process.

**Pro:** exact same behavior as desktop, full template support, single source of
truth for secret+template definitions.

**Con:** depends on HM module internals (`ExecStart` path). Pulls in a full HM
evaluation — closure likely includes nixpkgs and various HM dependencies even
though we only need a Go binary + a JSON file. **Startup latency risk for Claude
Code web.**

### Option B: Build the manifest directly

`sops-install-secrets` just reads a JSON manifest. Build it without HM:

```nix
let
  sops-install-secrets = sops-nix.packages.${system}.sops-install-secrets;
  manifest = pkgs.writeText "manifest.json" (builtins.toJSON {
    secrets = [{
      name = "buildbuddy_api_key";
      sopsFile = ./secrets/buildbuddy.yaml;
      key = "buildbuddy_api_key";
      path = "/run/secrets/buildbuddy_api_key";
      format = "yaml";
      mode = "0400";
    }];
    templates = [{
      name = "buildbuddy.bazelrc";
      content = "common --remote_header=x-buildbuddy-api-key=<SOPS:HASH:PLACEHOLDER>\nbuild --config=rbe\n";
      path = "$HOME/.config/bazel/buildbuddy.bazelrc";
      mode = "0600";
    }];
    placeholderBySecretName = { buildbuddy_api_key = "<SOPS:HASH:PLACEHOLDER>"; };
    ageKeyFile = "/run/secrets/age-key";
    # ...
  });
in
  pkgs.writeShellScript "activate-secrets" ''
    ${sops-install-secrets}/bin/sops-install-secrets -ignore-passwd ${manifest}
  ''
```

**Pro:** small closure (just the Go binary + manifest + encrypted sops file).
No HM dependency.

**Con:** manifest schema is undocumented and may change across sops-nix
versions. Placeholder hashes must be computed to match what
`sops-install-secrets` expects. Home-manager and container definitions are
structurally similar but not literally shared — they'd be two Nix expressions
consuming the same source-of-truth data (secret file path, YAML key, template
content).

### Option C: Thin wrapper around `sops` CLI

Skip `sops-install-secrets` entirely. Write a small script (shell or Python)
that does `sops -d --extract` + template substitution. The template content
and secret metadata are defined once in Nix and consumed by both HM (via
sops-nix module) and the container script.

```nix
# Shared definition (consumed by both)
secretDefs = {
  buildbuddy = {
    sopsFile = ./secrets/buildbuddy.yaml;
    key = "buildbuddy_api_key";
    template = ''
      common --remote_header=x-buildbuddy-api-key=@SECRET@
      build --config=rbe
    '';
    path = ".config/bazel/buildbuddy.bazelrc";
  };
};
```

Home-manager module translates `secretDefs` → `sops.secrets` + `sops.templates`.
Container gets a script that iterates `secretDefs`, runs `sops -d --extract`,
and does string substitution.

**Pro:** minimal closure (`sops` CLI is already installed in the web container
via `web_setup.sh`). Template content shared. No manifest schema dependency.

**Con:** two different activation mechanisms (sops-nix vs custom script) — but
the secret/template _definitions_ are shared, which is the part that actually
drifts. `sops` CLI is slower than `sops-install-secrets` (spawns a process per
secret vs one pass), but irrelevant for a handful of secrets.

### Option D: Do nothing

The current state has three implementations but they're all ~5 lines each and
the template is trivial (`common --remote_header=...`). The risk of drift is
low for a single secret. The complexity of any dedup solution may not pay for
itself until there are more shared secrets.

**Pro:** zero effort, no new abstraction.

**Con:** if more secrets get added (and there are already several —
`attic_token`, `ANTHROPIC_API_KEY`, `HF_TOKEN`, etc.), the drift risk grows.

## Closure size concern

Claude Code web containers download their Nix closure at session start. A
large closure = slower startup.

- **Option A** (full HM eval): likely pulls significant nixpkgs closure. Bad.
- **Option B** (`sops-install-secrets` only): the Go binary is statically
  linked (~15MB). Closure = binary + manifest + encrypted sops files. Small.
- **Option C** (`sops` CLI): already installed. Zero additional closure.
- The `sops` CLI binary is already present in the web container (installed by
  `web_setup.sh` via devShell). So Option C adds nothing to the closure.

## Age key in the container

The web container receives `SOPS_AGE_KEY` as an env var (set in the Claude
Code web UI, inherited by hook daemon). Both `sops` CLI and
`sops-install-secrets` respect this env var. No file mount needed.

Home-manager uses `~/.ssh/id_ed25519` (age via SSH key). The shared definition
would need to parameterize the key source — but that's already inherently
separate (HM sets `sops.age.sshKeyPaths`, container sets `SOPS_AGE_KEY` env).

## Full secret inventory

### Web session secrets

| Secret                          | SOPS file / source                           | Decryption                             | Output                          | Used for                   |
| ------------------------------- | -------------------------------------------- | -------------------------------------- | ------------------------------- | -------------------------- |
| `BUILDBUDDY_API_KEY`            | `secrets/buildbuddy.yaml`                    | `sops -d` via `_common.sh`             | env var + bazelrc template      | Bazel RBE                  |
| `GITHUB_TOKEN`                  | `secrets/github-pat-agentydragon-agent.yaml` | `sops -d` via `web_env.sh`             | env var                         | GitHub CLI, fork remote    |
| `DUCKTAPE_CI_READ_GITHUB_TOKEN` | `secrets/github-ci-read-pat.yaml`            | `sops -d` via `web_env.sh`             | env var                         | Reading GHA runs/artifacts |
| k8s SA token                    | `secrets/claude-web-k8s-jwt.yaml`            | `sops -d` in `write_kubeconfig_cli.py` | `~/.kube/config` (Python-built) | kubectl, MCP server        |
| `DUCKTAPE_OTEL_BEARER_TOKEN`    | k8s Secret `alloy-otlp-bearer-token`         | `kubectl get secret`                   | env var                         | OTEL traces to Alloy       |
| Docker mTLS key _(disabled)_    | `secrets/docker-ci/client-key.sops.pem`      | `sops -d` via `_common.sh`             | env var (base64)                | Docker CI mTLS             |

### Home-manager secrets (sops-nix)

| Secret               | SOPS file                                | Key                  | Output                                         | Type               | Hosts          |
| -------------------- | ---------------------------------------- | -------------------- | ---------------------------------------------- | ------------------ | -------------- |
| `buildbuddy_api_key` | `secrets/buildbuddy.yaml`                | `buildbuddy_api_key` | `~/.config/bazel/buildbuddy.bazelrc` + env var | template + sopsEnv | all            |
| `kubeconfig`         | `secrets/shared/kubeconfig.yaml`         | `kubeconfig`         | `~/.kube/config`                               | raw blob           | all            |
| `talosconfig`        | `secrets/shared/talosconfig.yaml`        | `talosconfig`        | `~/.talos/config`                              | raw blob           | all            |
| `attic_token`        | `secrets/home/<host>/attic.yaml`         | `attic_token`        | `~/.config/attic/config.toml`                  | template           | wyrm2, rugged  |
| `github_ssh_key`     | `secrets/home/<host>/github-ssh.yaml`    | `ssh_private_key`    | `~/.ssh/agentydragon_github_id_ed25519`        | raw file           | rugged, iguana |
| `ha_15leroy_ssh_key` | `secrets/15leroy-homeassistant-ssh.yaml` | `ssh_private_key`    | `~/.ssh/15leroy`                               | raw file           | wyrm2, rugged  |
| `HF_TOKEN`           | `secrets/shared/huggingface.yaml`        | `hf_token`           | env var                                        | sopsEnv            | all            |
| `HABITIFY_API_KEY`   | `secrets/shared/habitify.yaml`           | `habitify_api_key`   | env var                                        | sopsEnv            | all            |
| `ANTHROPIC_API_KEY`  | `secrets/home/wyrm2/anthropic.yaml`      | `anthropic_api_key`  | env var                                        | sopsEnv            | wyrm2          |
| `OPENAI_API_KEY`     | `secrets/home/wyrm2/openai.yaml`         | `openai_api_key`     | env var                                        | sopsEnv            | wyrm2          |
| `wyrm_ssh_key`       | `secrets/home/rugged/wyrm-ssh.yaml`      | `ssh_private_key`    | `~/.ssh/wyrm_agentydragon_user_id_ed25519`     | raw file           | rugged         |
| `vps_root_ssh_key`   | `secrets/home/rugged/vps-root-ssh.yaml`  | `ssh_private_key`    | `~/.ssh/vps_root_id_ed25519`                   | raw file           | rugged         |
| `vps_user_ssh_key`   | `secrets/home/rugged/vps-user-ssh.yaml`  | `ssh_private_key`    | `~/.ssh/vps_agentydragon_user_id_ed25519`      | raw file           | rugged         |

### Overlap analysis

Only **`BUILDBUDDY_API_KEY`** is truly duplicated across web and HM (same SOPS
file, same key, same output — 3 independent implementations). The k8s token
uses the same `sops -d` pattern but produces fundamentally different output
(HM: admin kubeconfig verbatim; web: scoped SA kubeconfig with runtime
proxy/CA injection). `OTEL_BEARER_TOKEN` comes from k8s, not SOPS.

Most HM secrets (SSH keys, talosconfig, attic) are local-machine-only and have
no web counterpart. The `sopsEnv` secrets (`HF_TOKEN`, `HABITIFY_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) are not currently needed in web sessions.

## Current recommendation

**Option C** is the sweet spot if this is worth doing at all: share the
secret/template _definitions_ in Nix, use sops-nix for HM activation and a
thin `sops -d` script for the container. Zero additional closure, `sops` is
already installed.

**Option D** (do nothing) is fine given that only BuildBuddy is truly
duplicated today. Revisit when Docker mTLS goes live or more secrets need
sharing.
