# Self-hosted Claude Code runtime — retired

Haku briefly ran Claude Code itself: haku-console provisioned a `haku-harness-runner` pod in
`haku-runtime-sandbox` from a `SandboxTemplate`, drove the CLI's stream-JSON wire over a
WebSocket bridge, and put a chat surface in front of it, on the operator's Claude subscription
(latterly through a LiteLLM key the egress fence substituted). It ran as a short experiment and
has been removed. What follows is what it established that a second attempt would otherwise
re-derive; Agent SDK facts are as of SDK 0.1.48 / Claude CLI 2.1.71 (2026-07-31).

## Credential

- **Subscription use is in policy for one operator.** The
  [legal-and-compliance doc](https://code.claude.com/docs/en/legal-and-compliance) scopes Pro/Max
  limits to "ordinary, individual usage of Claude Code and the Agent SDK"; its restriction targets
  developers routing plan credentials "on behalf of their users".
- **Headless subscription auth works although undocumented.** A `claude setup-token` token as
  `CLAUDE_CODE_OAUTH_TOKEN` authenticated multi-turn inference in a container with no login and
  no API key. The SDK's documented auth surface does not mention it.
- **The CLI works behind Haku's TLS-intercepting forced proxy** given `NODE_EXTRA_CA_CERTS`; no
  direct egress exception was needed.
- **Gotcha: the loop inverts a credential boundary.** Running it where Haku has CRUD puts the
  subscription token in reach of the agent it drives, and a subscription token has no per-lane kill
  switch. Keep the token outside the agent's namespace and substitute it at an egress fence, as the
  LiteLLM-key lane did.

## Agent SDK / CLI gotchas

- **Resume from a different `cwd` silently starts a fresh session.** Transcripts live at
  `$CLAUDE_CONFIG_DIR/projects/<cwd with non-alphanumerics as ->/<session-id>.jsonl`; pin the working
  directory. Python always persists them.
- **Tool restriction:** `allowed_tools` only auto-approves; unlisted tools stay visible.
  `disallowed_tools` with a bare name removes the definition. `allowed_tools` does not constrain
  `bypassPermissions`, and a bare-name allow skips `can_use_tool`. MCP-only is: deny built-ins by
  name, allow `mcp__<server>__*`, `permission_mode="dontAsk"`; a `PreToolUse` hook is the only gate
  that holds even under bypass.
- **Interrupt** works only in streaming mode and leaves a `ResultMessage` (`terminal_reason`
  `aborted_streaming`/`aborted_tools`) that must be drained before the next query.
- **Hooks are in-process callbacks.** Python lacks `SessionStart`, `SessionEnd`, `PostCompact`,
  `StopFailure` and `PermissionDenied`; none could observe pod loss anyway — watch the Sandbox CR.
- **`SessionStore` runs inside the sandbox**, so mirroring transcripts through it gives the fenced
  pod credentials for, and egress to, whatever store it writes.
- **Streaming tool arguments** need `include_partial_messages` plus
  `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1`; persist raw events before parsing.
- **OTel metrics need cumulative temporality**, or they vanish silently:
  <../../cluster/docs/lessons_learned/2026_07_31_claude_code_otel_delta_temporality.md>.
