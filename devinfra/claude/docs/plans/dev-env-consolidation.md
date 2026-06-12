# Dev Environment Consolidation

Most of this plan has landed. Keep this file only for the remaining cross-cutting
opportunities that do not fit cleanly in `devinfra/claude/TODO.md`.

## Resolved

- Repo-specific dev tools moved out of home-manager into `flake.nix`
  `devToolPackages`, shared by `devShells.default` and `packages.devtools`
  (`web_setup.sh` and CI install the latter).
- Bazelrc ownership is intentionally split:
  - home-manager `~/.bazelrc`: display prefs, platform, BuildBuddy credential
    `try-import`;
  - session-start `bazelrc.mako`: ephemeral proxy/JVM/RBE settings;
  - repo `.bazelrc`: build semantics.

## Remaining: Unified Secret Delivery

The web container has exactly one user session, so standard dotfile paths such
as `~/.config/bazel/buildbuddy.bazelrc` and `~/.kube/config` are enough. Both
home-manager and web setup want the same high-level operation: decrypt a SOPS
source, render a template or env export, and write the standard target path.

Target shape:

1. Define overlapping secret mappings once in Nix.
2. Generate an `activate-secrets` script using `sops` + `yq`.
3. Call that script from home-manager activation, `web_setup.sh`, and optionally
   the devShell shell hook.
4. Keep SSH keys and attic in sops-nix, because they are home-manager-only.

Do not replace the session bazelrc overlay: proxy credentials, JVM sizing, and
platform detection are still per-session.

Rejected paths remain rejected: running full home-manager in the web container
would add eval/download time, and trying to share a YAML config between Nix and
Python turns a tiny mapping into a custom secret-manager DSL.

## Remaining: Prettier Duplication

The devShell includes `nodePackages.prettier`, but pre-commit uses its own Node
environment with `prettier`, `prettier-plugin-svelte`, and `svelte`.

Consolidate only if first-run hook setup time or version drift becomes painful:
install prettier plus the Svelte plugin through Nix, switch the hook to
`language: system`, and wrap it with `NODE_PATH` so `.prettierrc.cjs` can
`require()` the plugin.
