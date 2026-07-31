# Agent: "public coder"

- **P1-P3**: no new research needed — `docs/self_hosted_coding_agent_platforms.md`
  already surveys this almost exactly, including an explicit "OpenClaw + GitHub
  workflow" recommendation (§"OpenClaw + GitHub workflow (cluster-current option)",
  lines 99-101) that matches P3's stated preference (plain OpenClaw, no sandboxing,
  GitHub-as-review-UI).
- **Tool-output truncation (C9)**: OpenClaw truncates single oversized tool
  results, verified by reading the source rather than the docs.
  (`docs/self_hosted_coding_agent_platforms.md` and the since-deleted
  `cluster/docs/openclaw_command_execution.md` were both stale on this and on the
  execution model; corrected in the same change as this doc.)
  - `src/agents/session-tool-result-guard.ts` intercepts every `toolResult`
    message before it's written to the session transcript and calls into
    `src/agents/embedded-agent-runner/tool-result-truncation.ts`, which does
    **conditional middle-truncation** (head+tail kept, middle dropped) when the
    last ~2000 chars look "important" (error/traceback/JSON-closing patterns, via
    `hasImportantTail()`; ~70% head / up to 4000 chars tail budget), or plain
    head-keep otherwise. Cap is `DEFAULT_MAX_LIVE_TOOL_RESULT_CHARS = 16,000`,
    auto-scaled by the model's context window (16K for <100K-token models,
    32K/≥100K, 64K/≥200K). The marker reports the omitted-char count plus an
    actionable hint (`"[... N more characters truncated; rerun with narrower args
if needed]"`). Test coverage in `src/agents/session-tool-result-guard.test.ts`.
    Applies to any `toolResult`-typed message regardless of source tool (exec,
    MCP, Codex-shaped blocks per PR #87912), not exec-specific. **No
    operator-facing config knob for the cap** — it's derived from the resolved
    model's context window.
  - This closed [#16574](https://github.com/openclaw/openclaw/issues/16574), where
    exec output had only a UI-display cap (`TOOL_RESULT_MAX_CHARS = 8000`, trimming
    what's shown but not what's sent to the model) and an `openclaw config` dump
    produced ~273K tokens that broke the session.
  - [#24920](https://github.com/openclaw/openclaw/issues/24920) and
    [#36964](https://github.com/openclaw/openclaw/issues/36964) are closed by an
    automated stale-bot, not by maintainer decision — `state_reason: not_planned`
    is the bot's label and neither thread has a substantive maintainer response.
    An unattended backlog, not an active "no."
  - **Live gap**:
    [#113701](https://github.com/openclaw/openclaw/issues/113701) ("Context
    Overflow: large tool outputs exceed context window, compaction can't recover",
    opened 2026-07-25, `P1`, unresolved) — the single-result guard doesn't catch
    **aggregate overflow**: several medium-sized tool outputs (a few `git diff`s,
    a verbose test run) accumulating within one turn can still blow the context,
    and mid-turn compaction can't recover.
  - Architecturally, OpenClaw is its own custom agent loop calling provider APIs
    directly (Anthropic/OpenAI/Gemini/Grok/OpenRouter/Copilot/MiniMax) — not a
    wrapper around Claude Code/Codex CLI by default, so it doesn't inherit their
    built-in truncation for the cases the single-result guard doesn't cover.

  **Net for public-coder**: the single-giant-output failure mode is fixed;
  many-medium-outputs-in-one-turn is a real, currently-open (P1) exposure, smaller
  in practice than what retired kagent hit (git/gh/build output on `ducktape`, not
  `kubectl logs`-style firehoses). Given P1 already accepts "simple, no
  sandboxing," standing up plain OpenClaw + #yolo is still the reasonable starting
  point; budget for occasionally restarting a wedged session, and keep the
  CLI-wrapping alternatives (`siteboon/claudecodeui`, `agent-sandbox` + `agentapi`)
  as the escape hatch if it wedges often enough to be annoying.

- GitHub bot identity (P2) is already established: `agentydragon-agent` PAT
  (`secrets/github-pat-agentydragon-agent.yaml`), used by Claude Code web, OpenClaw,
  and CI (`.sops.yaml:853`), consumed by OpenShell's GitHub credential provider
  (`cluster/k8s/agents/openshell/openclaw/github-credentials.yaml`). Reuse directly.
