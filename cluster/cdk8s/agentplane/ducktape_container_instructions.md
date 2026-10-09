You are using the ducktape-specific `runner-ducktape` container, built with Nix (not NixOS).
The repository's shared tool set is already on PATH: `bb`, `bbr`, Bazelisk, `pre-commit`,
Ruff, Prettier and its pinned plugins, nixfmt, Buildifier, and the repo-configured Gazelle.
Use these installed tools; do not install standalone substitutes or bypass repository hooks.
Use the checkout's normal commands and configuration, preferring `bbr` for remote builds/tests.
The Thread setup clones `devel` into its own workspace and installs the repo's Git hooks.
The image has neither `nix` nor `direnv`; do not run `direnv allow` here.

Outbound access still follows Agentplane egress policy. Check the current rules for credentials
and destinations; tool availability does not imply authenticated BuildBuddy access. Preserve the
sandbox's proxy and CA configuration, and report an unavailable route rather than bypassing it.

This is still a container sharing resources with the harness, not a VM with isolated tool
processes. A large local build or indiscriminate process kill can terminate your own session.
Prefer remote execution and target only processes you created when cleaning up.
