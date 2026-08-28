# Agent SDK loop in a Haku sandbox, driven from haku-console

Status: **built, and this plan is the record of why rather than a queue of work.** The 2026-07-31
Kubernetes probe resolved the two architecture-blocking mechanical questions — subscription OAuth
works headlessly through the Agent SDK, and the bundled Claude CLI works through Haku's
TLS-intercepting forced proxy — and the runtime that answer unblocked is running: the
`haku-claude` `SandboxTemplate` and its warm pool
(<../../cluster/k8s/haku/workspaces/app/sandboxtemplate-haku-claude.yaml>), the in-sandbox bridge
(<../runtime/x/bridge/>), the console's session runtime and its chat surface
(<../console/x/session_runtime.py>, `frontend/x/`), and a Matrix room in front of all of it
(<../console/channels/matrix/SPEC.md>). One decision below was reversed by the build: no Python imports the
Agent SDK any more — the console drives Claude Code's wire itself
(<cli_protocol_ownership.md>) — so the SDK survives only as the wheel the CLI binary is
extracted from.

What is **not** built is named per item in _What this does not prove_ and _Open questions_; those
are the live parts of this file.

Companion to [runtime_options.md](runtime_options.md), which catalogues this as the
"Runtime A variant — self-hosted Claude Code (Agent SDK)". The companion move of where a run's
_commands_ execute landed (<../runtime/claude_web_env/run.md> § Commands run in the in-cluster
sandbox); this plan moves the **agent loop itself** into the sandbox and puts a chat UI in
front of it.

## The shape

A session-oriented UI in haku-console. Starting a session provisions a sandbox CR, sets up
Haku inside it, starts an Agent SDK loop, and gives the operator a chat window — usable from
a phone. The motivation is the one Claude Code web can't serve: **full telemetry and
transcripts** for Haku runs, which today can only be extracted by asking the agent to upload
them by hand.

## Which credential the loop uses

The premise is "run Haku on the Anthropic subscription rather than paying API rates."
**Policy permits this for individual use**, per the
[legal-and-compliance doc](https://code.claude.com/docs/en/legal-and-compliance):

> Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude
> Code **and the Agent SDK**.

Its restriction is aimed at a different audience — "third-party developers" building products,
who may not "offer Claude.ai login or … route requests through Free, Pro, or Max plan
credentials **on behalf of their users**". A single operator running their own agent on their
own subscription is the named-in-scope case. The Agent SDK overview's blunter "use the API key
authentication methods described in the Quickstart instead" is guidance for that developer
audience, not a prohibition on the individual case.

What remained was **mechanical, not legal**: `CLAUDE_CODE_OAUTH_TOKEN` appears nowhere in the
SDK's documented auth surface. `ClaudeAgentOptions` has no auth fields; credentials reach the
CLI subprocess only through the inherited environment or `options.env`, and subscription OAuth is
built around an interactive laptop login — so whether a `claude setup-token` token authenticates a
**headless container** was the thing that could still have invalidated the plan. It does;
_Compatibility result_ below is the measurement, and the live session has exercised the same path
continuously since.

The real design cost is elsewhere and is covered in
[runtime_options.md](runtime_options.md): running the loop in `haku-sandbox` **inverts a
deliberate credential boundary**, putting the subscription OAuth token in a namespace where
Haku has full CRUD, when today the launch credential lives in `haku-console` where Haku has no
RBAC. Misuse is enforceable against the personal Anthropic account without prior notice, and
there is no per-lane kill switch the way a LiteLLM virtual key would give. Mitigations exist —
loop in a third namespace; built-in tools disabled so it reaches `haku-sandbox` only through
MCP (which is the tool-surface decision below) — but they are design work, not defaults.

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

**Remote transport.** The Python SDK launches the CLI with `--input-format stream-json` and
`--output-format stream-json`; its `Query` layer implements hooks, interrupts, SDK-hosted MCP, and
message routing over an abstract `Transport`. The plan was to keep `ClaudeSDKClient` and `Query` in
the trusted console process and tunnel that JSON protocol over a WebSocket to a thin sandbox bridge
around Claude Code's stdin/stdout, deriving a versioned launch frame from `ClaudeAgentOptions` and
pinning it against the SDK's `SubprocessCLITransport` with a compatibility test.

**Built, and the SDK is out of the loop entirely** — <cli_protocol_ownership.md> is the decision and
its reasoning. The bridge is `//haku/runtime/x/bridge:runner_bin`, which starts the pinned Claude
Code executable the sandbox image supplies; the console drives the wire itself
(`console/x/claude_code/client.py` replaces `ClaudeSDKClient`, `runtime/x/bridge/claude_options.py` replaces `ClaudeAgentOptions` plus that private argv
builder, and `test_claude_options.py` pins the argv where the compatibility test used to). The WebSocket
still adds only launch and lifecycle framing — it defines no second prompt, turn, or tool protocol —
and the SDK wheel survives as a build dependency for one reason: `extract_claude.py` pulls the CLI
binary out of it.

The runtime explicitly enables both `include_partial_messages` and
`CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1`. The former exposes raw Anthropic stream events;
the latter makes tool arguments arrive as incremental `input_json_delta` events rather than only as
the final `ToolUseBlock`. Console persistence must retain the raw events before typed parsing, then
project complete tool inputs/results from final messages and `PreToolUse`/`PostToolUse` hooks.

## Compatibility result — 2026-07-31

The probe itself is gone (removed 2026-08-10); this section is the record of what it
established, which is the part worth keeping. It was a spike, its question is answered, and
the console's own long-lived session now exercises the same authentication path
continuously — with a louder symptom, since a broken token means the chat surface stops
answering. What was lost with it is isolation: an SDK or CLI bump that breaks subscription
OAuth will now show up as "the room went quiet" rather than as a named 18-second failure.
Worth rebuilding as a deliberate scheduled check if that trade turns out to bite; it should
not come back as a Job that re-runs whenever an unrelated image tag moves.

The one-shot `haku-agent-sdk-smoke` Job from PR #3632 completed successfully in
`haku-claude-sandbox` against source commit `da578377`. The pod exited 0 with no restart, and the Job reached
`Complete` in 11 seconds. It ran Agent SDK 0.1.48 with its bundled Claude CLI 2.1.71; transcript
inspection showed the CLI selected `claude-sonnet-4-6` under the subscription credential.

The run proves:

- **Headless subscription authentication works.** A long-lived token from `claude setup-token`,
  injected as `CLAUDE_CODE_OAUTH_TOKEN`, completed three real inference turns without interactive
  login or an Anthropic API key.
- **The existing egress fence is compatible.** The CLI reached Anthropic through
  `haku-egress-proxy` with `NODE_EXTRA_CA_CERTS` and the injected CA bundle. Successful inference
  proves both proxy routing and TLS interception compatibility; no direct egress exception was
  needed.
- **Streaming works through `ClaudeSDKClient`.** Every turn emitted seven partial
  `StreamEvent`s before a successful terminal `ResultMessage` with usage, latency, cost, and stop
  metadata.
- **Same-client conversational state works.** A second turn recalled a random nonce from the
  first turn while retaining one stable session ID.
- **Disk-backed resume works at a pinned working directory.** After the first client closed, a
  new client resumed by session ID at `/workspace`, recalled the first-turn nonce, and retained
  the same session ID.
- **The transcript is a real, usable Claude Code JSONL.** The probe found it under
  `$CLAUDE_CONFIG_DIR/projects/-workspace/<session-id>.jsonl`; manual inspection of a rerun showed
  queue, user, and assistant records with the expected session ID, `cwd`, CLI version,
  `dontAsk` permission mode, model response, and usage metadata.
- **The Python hook surface needed for the runtime is active.** `UserPromptSubmit` and `Stop`
  each fired exactly once for all three turns. `PreToolUse` fired zero times because the probe
  exposed no tools; the deny-all backstop remained installed.
- **The pod retained the intended containment.** It ran non-root with all capabilities dropped,
  no privilege escalation, no mounted Kubernetes service-account token, emptyDir-backed state,
  and no SDK tools.

No credential appeared in the structured logs or inspected transcript excerpt. The observed
three-turn model cost was about USD 0.00525, although subscription accounting rather than the
reported API-equivalent cost is the premise of this runtime.

### What this does not prove

- **OTel arrival is not yet verified.** The probe proved that the bearer, endpoint, cumulative
  temporality, resource attributes, and exporters were passed through `ClaudeAgentOptions.env`,
  but the corresponding logs/metrics/traces have not yet been located in the telemetry backend.
- **Resume survived a client/process boundary, not a pod loss.** The transcript lived on an
  `emptyDir`; a replacement pod would lose it. The real runtime needs a deliberate persistence or
  transcript-export design if pod-level recovery is required.
- **Interrupt/cancellation was not exercised.** The chat runtime needs a focused
  `client.interrupt()` test that drains the terminal result before accepting the next prompt.
- **MCP wiring was not exercised.** The probe intentionally exposed no tools. A harmless MCP call
  should verify server auth, tool naming, `PreToolUse`, and haku-console approval behavior before
  broadening the production tool surface.
- **Long-term token operations are not proven by an 11-second run.** Expiry, revocation, rotation,
  and a stale-token failure mode still need an operational canary/runbook.

### Decision

Proceed with the runtime build. The failures that could have invalidated the architecture —
headless OAuth rejection and incompatibility with the forced TLS proxy — did not occur. Verify
OTel ingestion now because it is a cheap check against the recorded run ID. Treat interrupt,
single-tool MCP, pod-loss persistence, and token-rotation coverage as acceptance tests for their
respective implementation slices rather than reasons to defer the image, `SandboxTemplate`, or
first console session flow.

## Architecture sketch

```text
haku-console  ── session records, chat UI, MCP + approval queue
     │
     │ provision / dispose (sandbox tools)
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

The cumulative-temporality fix and its write-up landed on `devel` in PR #3630. The spike passed
that setting through to the SDK subprocess, but backend ingestion still needs the explicit check
described above.

## Build order

1. **Spike — complete.** PRs #3631 and #3632 deployed the credential and one-shot compatibility
   Job. Headless OAuth, intercepted egress, streaming, multi-turn state, same-pod disk resume,
   transcript creation, and Python hooks passed. OTel configuration passthrough passed; backend
   arrival remains to be checked.
2. **Image + `SandboxTemplate` — done.** `haku-harness-runner`, the `haku-claude` template and its
   warm pool, in `cluster/k8s/haku/workspaces/app/`.
3. **Proxy body capture**, which is a config change to already-deployed mitmproxy. **Not verified
   here** — nothing in this repo records it landing.
4. **Thinnest console surface — done, and long since overtaken.** The console has a full session
   surface (`frontend/x/`) and a Matrix room in front of the same sessions.

## Out of scope

**`SessionStore`.** The SDK's adapter for mirroring transcripts off-box is the obvious fit for
haku-traces, but it runs _inside_ the sandbox: pointing it at the console's Postgres would give
a deliberately fenced pod egress to, and credentials for, a database outside its perimeter.
Deferred with the alternative (console pulls the local JSONL, or it ships over the permitted
MCP path) and the behavioral gotchas recorded in <../TODO.md> § haku-traces.

## Open questions

- Whether the console should render an approval queue for the in-sandbox agent's MCP calls, or
  whether an in-sandbox loop is trusted enough to auto-approve what the console currently gates.
  **Settled by construction, not by decision:** the session launches
  `--permission-mode bypassPermissions` with no `setting_sources` (`bridge/claude_options.py`), so the
  CLI's own gate is off and MCP is reached as an external HTTP server the CLI contacts itself —
  which puts every call through the console's existing approval path and `auto_approval.py`, not a
  second queue.
- **Answered:** the session record is a table in the console DB — `sessions` and the
  `session_{messages,frames,turns,prompts,events,outbox}` around it — with the Sandbox CR held
  beside it as a claim (`x/sandbox_claims.py`).
- Whether an idle session should dispose its sandbox automatically, and what the operator sees
  when it does. **Still open, and now designed rather than merely asked:**
  <../console/plans/conversation_layers.md> § 9's conversation-owned prompt queue is the answer
  this question was waiting for; today an idle room holds a sandbox indefinitely.
