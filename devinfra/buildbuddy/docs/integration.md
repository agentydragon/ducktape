# BuildBuddy setup by environment

See [Bazel configuration and execution policy](../../docs/bazel_configuration.md#policy)
for rc ownership and RBE selection.

- **GitHub Copilot.** The [setup workflow](../../../.github/workflows/copilot-setup-steps.yml)
  passes the Actions secret to the [Bazel setup action](../../../.github/actions/setup-bazel/action.yml).
  With a key, [setup.sh](../setup.sh) writes `~/.config/bazel/buildbuddy.bazelrc` and imports it
  from `~/.bazelrc`. Without a key, the action still restores the GitHub Actions repository cache.
  Ducktape's RBE actions still require BuildBuddy authentication.
- **Claude Code.** [SessionStart](../../claude/claude_hook/main.rs) writes and imports a
  per-session `buildbuddy.bazelrc` when its credential overlay includes `BUILDBUDDY_API_KEY`.
  The credential applies to `rbe`. See [local bb startup and session rc selection](bazelrc_startup.md)
  and [session setup recovery](../../claude/claude_hook/docs/session_start_recovery.md).
- **Codex Cloud (Legacy).** The [bootstrap script](../../codex_cloud/README.md#what-this-setup-does)
  runs the shared setup script when it can resolve a key and wires the credential
  import into its Bazel configuration.
