# Dev Environment Consolidation

Reduce duplication between the three dev environment contexts that currently
configure overlapping tooling independently.

## Contexts

| Context                                   | Activation mechanism                        | Where defined                                |
| ----------------------------------------- | ------------------------------------------- | -------------------------------------------- |
| **Local dev machine** (agentydragon, gpd) | `home-manager switch`                       | `nix/home/`                                  |
| **Claude Code Web**                       | Session-start hook → `CLAUDE_ENV_FILE`      | `devinfra/claude/hook_daemon/session_start/` |
| **Claude Code CLI** (local)               | home-manager + session-start hook (lighter) | Both                                         |

## Resolved: tool lists

Commit `43b4e1ca` removed repo-specific dev tools from home-manager. They now
live in `flake.nix` as `devToolPackages`, shared between:

- `devShells.default` — for local `nix develop` / direnv
- `packages.devtools` — installable via `nix profile install .#devtools` (used by
  `web_setup.sh` and CI)

Home-manager only carries user-level tools (editor, shell, GUI, TUI utilities,
language toolchains) that aren't repo-specific.

## Resolved: bazelrc

No semantic overlap between the two bazelrc generators:

- **Home-manager `~/.bazelrc`**: display prefs (`--show_progress_rate_limit`,
  `--progress_in_terminal_title`), platform, `try-import` for BuildBuddy creds
- **Session-start `bazelrc.mako`**: proxy/JVM/RBE settings (ephemeral, per-session)
- **Repo `.bazelrc`**: build semantics (bzlmod, lint aspects, RBE config, etc.)

## Remaining opportunity: unified secret delivery

### Current state

Secrets are delivered through multiple mechanisms:

**Session start hook** (3 phases):

1. **SOPS → memory**: `buildbuddy_api_key`, `github_token`, `k8s_token`
2. **K8s API → memory**: `otel_bearer_token` (fetched using k8s_token from Phase 1)
3. **Memory → per-session files**: `buildbuddy.bazelrc`, env vars, kubeconfig

**Home-manager** (sops-nix at activation):

- `buildbuddy_api_key` → `~/.config/bazel/buildbuddy.bazelrc` (via `sops.templates`)
- `openai_api_key`, `anthropic_api_key`, etc. → env vars (via custom `sops-env` module)
- SSH keys → `~/.ssh/`
- `attic_token` → `~/.config/attic/config.toml`

### Key insight: global dotfiles work for web too

The web container has exactly one user session — there's no reason to write
secrets to per-session paths. Standard dotfile paths (`~/.config/bazel/buildbuddy.bazelrc`,
`~/.kube/config`) work and are what tools expect.

Both contexts want: **SOPS file → decrypt → template → standard dotfile path**.

| Secret               | SOPS source                       | Target path                          |
| -------------------- | --------------------------------- | ------------------------------------ |
| `buildbuddy_api_key` | `secrets/buildbuddy.yaml`         | `~/.config/bazel/buildbuddy.bazelrc` |
| `kubeconfig`         | `secrets/claude-web-k8s-jwt.yaml` | `~/.kube/config`                     |
| `otel_bearer_token`  | new SOPS file                     | env var or OTEL config               |
| `openai_api_key`     | `secrets/home/*/openai.yaml`      | `$OPENAI_API_KEY` env var            |
| `github_token`       | `secrets/github-pat-*.yaml`       | `$GITHUB_TOKEN` env var              |

### Proposed approach: Nix-generated activation script

Define secret mappings once in Nix. Generate a shell script that decrypts SOPS
files and renders templates to dotfile paths. Both contexts invoke this script.

```nix
# nix/modules/secrets.nix — single source of truth
let
  secrets = [
    { sopsFile = "secrets/buildbuddy.yaml"; key = "buildbuddy_api_key";
      target = ".config/bazel/buildbuddy.bazelrc";
      template = "common --remote_header=x-buildbuddy-api-key=__VALUE__\nbuild --config=rbe"; }
    { sopsFile = "secrets/github-pat-agentydragon-agent.yaml"; key = "github_token";
      envVar = "GITHUB_TOKEN"; }
    # ...
  ];
  activateSecrets = pkgs.writeShellScript "activate-secrets" ''
    # sops -d + yq for each secret, render templates / write env exports
  '';
in ...
```

Consumers:

- **Home-manager**: `home.activation.secrets = "${activateSecrets} $REPO_ROOT";`
  — replaces sops-nix for these secrets
- **Web `web_setup.sh`**: `activate-secrets /path/to/repo` — same script from
  the devtools closure
- **devShell shellHook**: `activate-secrets .` — optional, for local dev

### Rejected alternatives

**sops-nix in both contexts**: sops-nix's home-manager module uses a systemd
user oneshot service (`sops-install-secrets`) for activation. The web container
doesn't have a systemd user session (no PAM, no dbus, no `XDG_RUNTIME_DIR`).
Starting `systemd --user` manually is possible but more trouble than it's worth
for a few `sops -d` calls.

**home-manager in web container**: `home-manager switch` evaluates the full flake
including nixpkgs (~200MB+ download, significant eval time). The current approach
(`nix profile install .#devtools`) uses a pre-built closure from the binary cache
with no eval. Adding home-manager to the web session start path would be a major
regression in startup time.

**Shared config.yaml consumed by both Nix and Python**: The secret _sources_
(SOPS file, key) can be declared in YAML, but the _outputs_ (template content,
target path, env var name) can't — they're inherently different shapes. Extending
config.yaml with output declarations builds a mini secret-manager DSL for ~5
secrets.

### What stays different

- **Auth proxy, TLS CA, supervisor, Docker, platform detection**: inherently
  ephemeral/dynamic, no home-manager equivalent.
- **Session bazelrc overlay**: proxy/JVM settings must be per-session (proxy
  credentials rotate, platform detection varies).
- **SSH keys**: home-manager only (web sessions use HTTPS + tokens).

### Migration path

1. **Move `otel_bearer_token` to SOPS file** — eliminate k8s secret dependency.
   Removes the `kubernetes` Python dependency from the hook.
2. **Write `buildbuddy.bazelrc` to global path** — match home-manager's
   `~/.config/bazel/buildbuddy.bazelrc` instead of per-session path.
3. **Write kubeconfig to `~/.kube/config`** — same path in both contexts.
4. **Build `activate-secrets` script in Nix** — single secret declaration,
   generated shell script using `sops` + `yq`.
5. **Replace sops-nix** in home-manager with `home.activation` calling the
   same script (for the secrets that overlap — SSH keys and attic stay in
   sops-nix since they're home-manager-only).
6. **Remove k8s client setup** from session start hook.

## TODO: prettier duplication

The devShell includes `nodePackages.prettier`, but the pre-commit hook uses
`language: node` with its own `additional_dependencies` (prettier,
prettier-plugin-svelte, svelte). Pre-commit manages its own prettier
installation with plugins, ignoring the devShell copy entirely.

Should consolidate: install prettier with svelte plugin via devShell/Nix and
switch the pre-commit hook to `language: system`. This would:

- Remove the `additional_dependencies` download on first pre-commit run
- Ensure the same prettier version is used everywhere
- Remove `nodePackages.prettier` duplication (Nix vs pre-commit's node env)

Blocker: need to figure out how to make Nix's prettier find the svelte plugin.
The pre-commit hook's `.prettierrc.cjs` uses `require()` to load the plugin,
which resolves via `NODE_PATH` set by pre-commit's node environment. A Nix
wrapper would need to set `NODE_PATH` to include the plugin package.

## Oddities noticed in pre-commit config

- **`checkov`** pins rev `3.2.513` with `additional_dependencies: ["urllib3"]`.
  The urllib3 dep is a workaround (checkov probably has a broken transitive dep).
  This is fine but worth checking if newer checkov versions fix it.

- **`markdownlint-cli2`** is an external Node repo, not `language: system`. Could
  move to devShell like the other tools, but it's only scoped to `cluster/` and
  `website/` — low priority.

- **`block-specimen-code-changes`** and **`validate-specimen-issue-ids`** use
  `language: python` — pre-commit creates a venv for each. These are simple
  scripts that could be `language: system` if they don't need extra deps.

## Status

| Item                | Status                                            |
| ------------------- | ------------------------------------------------- |
| Tool lists          | resolved (shared `devToolPackages`)               |
| devShell ↔ devtools | resolved (shared list)                            |
| Bazelrc             | resolved (cleanly separated)                      |
| Secret delivery     | **opportunity** — Nix-generated activation script |
| Prettier            | **TODO** — deduplicate devShell vs pre-commit     |
| Pre-commit install  | harmless overlap                                  |
