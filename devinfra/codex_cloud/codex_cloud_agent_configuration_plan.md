# Verify the Ducktape environment in Codex Cloud (Legacy)

The [bootstrap guide](README.md) owns the supported commands, shell setup and
credential lifecycle. The following checks require an actual legacy environment.

- Configure both scripts. Run a fresh task and a cached resume. Confirm the
  agent shell finds the intended Nix tools and that `bbr` completes a test.
  Record setup time, cache recovery, and any observed timeout or resource
  ceiling with the plan tier. Evaluate `nix develop` or a different bootstrap
  only if the profile misses a required tool or setup exceeds the measured limit.
- Configure the runtime age key only for an environment where tasks may use it.
  Restrict agent egress to required destinations and exercise BuildBuddy and the
  package services from a task. Add environment defaults such as `CI` or
  `CODEX_AGENT` only when a tool requires them.
- Keep startup independent of Codex project hooks. To establish legacy hook
  behavior, configure a minimal hook with an observable local side effect and
  run no-op tasks in fresh and resumed environments. Record which events fired,
  their counts, and what survived cache resume. The current Cloud hooks guide
  describes a separate runtime. [Hooks](https://learn.chatgpt.com/docs/hooks)
