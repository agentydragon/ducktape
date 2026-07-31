# Agent SDK loop in a Haku sandbox, driven from haku-console

Status: **design only — blocked on an auth decision (see below). No code written.**

Companion to [runtime_options.md](runtime_options.md), which catalogues this as the
"Runtime A variant — self-hosted Claude Code (Agent SDK)", and to
[sandbox_run_runtime.md](sandbox_run_runtime.md), which moved where a run's _commands_
execute. This plan moves the **agent loop itself** into the sandbox and puts a chat UI in
front of it.

## The shape

A session-oriented UI in haku-console. Starting a session provisions a sandbox CR, sets up
Haku inside it, starts an Agent SDK loop, and gives the operator a chat window — usable from
a phone. The motivation is the one Claude Code web can't serve: **full telemetry and
transcripts** for Haku runs, which today can only be extracted by asking the agent to upload
them by hand.

## Blocking question: which credential the loop uses

The entire premise is "run Haku on the Anthropic subscription rather than paying API rates."
The Agent SDK documentation does not support that reading:

> Unless previously approved, Anthropic does not allow third party developers to offer
> claude.ai login or rate limits for their products, including agents built on the Claude
> Agent SDK. Use the API key authentication methods described in the Quickstart instead.
>
> — <https://code.claude.com/docs/en/agent-sdk/overview>

`CLAUDE_CODE_OAUTH_TOKEN` appears nowhere in the SDK's documented auth surface;
`ClaudeAgentOptions` has no auth fields at all, and credentials reach the CLI subprocess only
through the inherited environment (or `options.env`).

The note is scoped to _third-party developers offering claude.ai login for their products_,
which is not obviously personal single-operator use — but the instruction that follows is
unqualified. **Resolve this before building.** Every step below is wasted if the answer is
"API key only", because the cost argument was the reason to prefer this over Runtime C.

## Decisions taken

| Question                | Decision                                                                                                                                                               |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Language                | **Python.** Fits the repo's `py_library`/`py_test` + mypy/ruff aspects; TypeScript would pull in the `ts_library`/pnpm path for no gain.                               |
| Egress fence            | **Keep the existing `haku-sandbox` force-proxy → mitmproxy.** iron-proxy is wanted eventually but is not central; deferring avoids re-plumbing a load-bearing control. |
| Tool surface            | **MCP-only.** Not a privilege question — the `haku` SA is Haku's own identity and grants nothing new (the SandboxTemplate says as much) — but a gating and audit one.  |
| Session ↔ sandbox       | **1:1, pinned until disposed.** A timeout is acceptable _provided it is noticed_.                                                                                      |
| Transcripts + telemetry | **Capture everything**, including Anthropic request/response bodies via the proxy.                                                                                     |
| Template                | **New `SandboxTemplate`**, not a change to `haku`, whose header documents "It does NOT run the agent loop."                                                            |
| Image                   | **New image** baking the Agent SDK and the Python harness.                                                                                                             |

## What the SDK provides

Researched 2026-07-31 against <https://code.claude.com/docs/en/agent-sdk/>. Recorded here so
it does not have to be re-derived.

**Conversation.** `ClaudeSDKClient` is the multi-turn interface (async context manager,
`query()` + `receive_response()`, session ID tracked internally) — a direct fit for a pinned
sandbox. `include_partial_messages=True` yields `StreamEvent` messages carrying partial text,
and `client.interrupt()` stops mid-turn. Interrupt works **only in streaming mode**, does not
clear the buffer, and requires draining the interrupted task's `ResultMessage` (whose
`terminal_reason` is `aborted_streaming` or `aborted_tools`) before the next query.

**Sessions.** Resume by ID (captured from `ResultMessage.session_id`), or `fork_session=True`
to branch. Transcripts are JSONL under
`$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session-id>.jsonl`, where `<encoded-cwd>` is the
absolute working directory with every non-alphanumeric character replaced by `-`. **A resume
from a different `cwd` silently starts a fresh session** rather than erroring — pin the
working directory. Python always persists to disk (`persist_session: False` is TypeScript-only).

**Restricting tools.** `allowed_tools` only _auto-approves_; unlisted tools remain visible to
the model and fall through to the permission mode. `disallowed_tools` with a **bare name**
removes the tool definition from the request entirely, and accepts globs (`"*"`, `"mcp__*"`).
Allow rules take a glob only after a literal `mcp__<server>__` prefix. So MCP-only is: deny
the built-ins by name, allow `mcp__<server>__*`, and set `permission_mode="dontAsk"` so
anything unmatched is denied rather than prompted. Two traps: `allowed_tools` does **not**
constrain `bypassPermissions`, and a bare-name allow entry skips the `can_use_tool` callback
entirely. A `PreToolUse` hook runs before every other step and can deny even under bypass —
that is the reliable gate.

**Hooks are in-process callbacks** (`options.hooks`), not `type: "command"` shell hooks. This
is the capability Claude Code web lacks and the reason the memory-flush idea becomes ordinary
code. Python has `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`,
`Stop`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PermissionRequest`, and
`Notification`. Roughly a third of the hook surface is **TypeScript-only**, including
`SessionStart`, `SessionEnd`, `PostCompact`, `StopFailure`, and `PermissionDenied`.

`SessionEnd`'s absence costs nothing here: an in-process hook cannot fire when the _pod_ dies,
so sandbox loss was never detectable that way. Watch the Sandbox CR instead, and use
`ResultMessage.terminal_reason` for in-band ends.

**Environment.** `options.env` is passed to the CLI subprocess, so telemetry is configured
per session rather than environment-wide — which is what failed in the Claude Code web
environment.

## Architecture sketch

```text
haku-console  ── session records, chat UI, MCP + approval queue
     │
     │ provision / dispose (sandbox_mcp)
     ▼
Sandbox CR (new template)  ── pod running the Python harness
     │                            └─ ClaudeSDKClient, tools = MCP-only
     │ egress: haku-sandbox force-proxy CCNP
     ▼
mitmproxy ── Anthropic request/response bodies captured here
     │
     ▼
api.anthropic.com
```

Telemetry lands two ways, deliberately: the proxy captures exact wire bodies, while OTEL from
the CLI carries structured events, token counts, and cost. The OTEL leg needs
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative`; Claude Code defaults to delta
temporality, which `otelcol.exporter.prometheus` drops silently, so the metrics simply never
arrive and nothing reports an error. `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH` separately truncates
inline bodies at 61440 bytes by default, which is why the proxy leg carries the full ones.

> **This is not yet fixed on `devel`.** The temporality variable and its write-up were
> committed 2026-07-31 but the branch was never merged, so no laptop picks it up from
> home-manager. Confirm it has landed before relying on the OTEL leg here.

## Build order

0. **Resolve the auth question.** Nothing below is worth doing first.
1. **Spike.** One pod, one Agent SDK turn, subscription-authed, egressing through the proxy —
   `kubectl apply`, one response, delete. No UI, no session model, no CR plumbing. It tests the
   two things nothing in this repo has exercised: whether `CLAUDE_CODE_OAUTH_TOKEN`
   authenticates **headless in a container** (subscription OAuth is built around interactive
   `claude setup-token` on a laptop), and whether the CLI tolerates **TLS interception**. It
   should also report whether the CLI's OTEL variables take effect under the SDK — inferred
   from `options.env` passthrough, not verified.
2. **Image + `SandboxTemplate`**, once the spike passes.
3. **Proxy body capture**, which is a config change to already-deployed mitmproxy.
4. **Thinnest console surface** — a "new session" button and one text box.

## Out of scope

**`SessionStore`.** The SDK's adapter for mirroring transcripts off-box is the obvious fit for
haku-traces, but it runs _inside_ the sandbox: pointing it at the console's Postgres would give
a deliberately fenced pod egress to, and credentials for, a database outside its perimeter.
Deferred with the alternative (console pulls the local JSONL, or it ships over the permitted
MCP path) and the behavioral gotchas recorded in <../TODO.md> § haku-traces.

## Open questions

- Whether the console should render an approval queue for the in-sandbox agent's MCP calls, or
  whether an in-sandbox loop is trusted enough to auto-approve what the console currently gates.
- Where the session record lives — a new table in the console DB, or derived from Sandbox CRs.
- Whether an idle session should dispose its sandbox automatically, and what the operator sees
  when it does.
