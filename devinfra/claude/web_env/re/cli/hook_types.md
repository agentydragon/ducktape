# Claude Code Hook Types: Prompt & Agent Hooks

**Binary**: Claude Code CLI v2.1.79 (Bun-compiled ELF, `@anthropic-ai/claude-code`)
**Build time**: 2026-03-18T21:33:25Z
**Source**: Reverse-engineered from `/opt/claude-code/bin/claude` (236 MB Bun single-file executable)

## Overview

Claude Code supports four hook types: `command`, `prompt`, `agent`, and `http`.
This document covers the LLM-based types (`prompt` and `agent`) and what context
they receive.

## Hook Configuration Schema

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "...", "timeout": 30 },
          { "type": "prompt", "prompt": "Is this safe? $ARGUMENTS", "model": "claude-sonnet-4-6" },
          { "type": "agent", "prompt": "Verify tests pass: $ARGUMENTS", "model": "claude-sonnet-4-6", "timeout": 60 },
          { "type": "http", "url": "https://...", "timeout": 30 }
        ]
      }
    ]
  }
}
```

Prompt and agent hooks are only available for: `PreToolUse`, `PostToolUse`,
`PermissionRequest`.

## `$ARGUMENTS` Substitution

Both prompt and agent hooks use `$ARGUMENTS` as a placeholder in their `prompt`
field. This is replaced with the **hook input JSON** — the same JSON that
`command` hooks receive on stdin.

The substitution is done by function `cu$` (deobfuscated: `substituteArguments`),
which calls `VEH` (deobfuscated: `replaceArgumentsInString`). This performs:

1. `$ARGUMENTS[N]` — replaced with the Nth token from the input
2. `$N` — replaced with the Nth token
3. `$ARGUMENTS` — replaced with the full JSON string

If the prompt already contains `$ARGUMENTS` and it gets substituted, the full
JSON is inlined. If the prompt has no `$ARGUMENTS` and the input is non-empty,
the input is appended after a newline.

## Common Hook Input (what `$ARGUMENTS` contains)

All hooks receive a JSON object with these base fields (function `y4` /
`createBaseHookInput`):

```typescript
{
  session_id: string,
  transcript_path: string,   // Path to JSONL conversation transcript
  cwd: string,               // Current working directory
  permission_mode: string,   // "default", "plan", "acceptEdits", etc.
  agent_id: string | null,   // Subagent ID if in subagent context
  agent_type: string | null   // Agent type name
}
```

Plus event-specific fields:

- **PreToolUse**: `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`
- **PostToolUse**: `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, `tool_response`
- **PermissionRequest**: `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`

**Key insight**: The hook input JSON contains `transcript_path` — the path to
the full JSONL conversation transcript file. The LLM itself does NOT
automatically see the transcript, but the agent hook's system prompt tells it
about this file so it can read it.

---

## Prompt Hook (`type: "prompt"`)

### Execution Function

Minified name: `itf`
Module: `rtf` (hook prompt runner)

### What It Does

A **single-turn LLM call** with no tool access. The model evaluates a condition
and returns `{ok: true}` or `{ok: false, reason: "..."}`.

### System Prompt (verbatim from binary)

```
You are evaluating a hook in Claude Code.
Your response must be a JSON object matching one of the following schemas:
1. If the condition is met, return: {"ok": true}
2. If the condition is not met, return: {"ok": false, "reason": "Reason for why it is not met"}
```

### Messages Sent to the LLM

The prompt hook sends **only**:

1. **System prompt**: The 3-line evaluation prompt above
2. **User message(s)**: The hook's `prompt` field with `$ARGUMENTS` substituted
   - If `_` (contextMessages) is provided and non-empty: `[...contextMessages, userMessage]`
   - Otherwise: `[userMessage]`

**No conversation transcript is included.** The model sees only the substituted
prompt (which contains the hook input JSON).

### Model & Configuration

- **Model**: `H.model ?? Pw()` — uses the hook's configured `model` field, or
  falls back to `Pw()` (the default small/fast model, typically Haiku)
- **Thinking**: Disabled (`thinkingConfig: {type: "disabled"}`)
- **Output format**: Constrained JSON schema (`{ok: boolean, reason?: string}`)
- **Timeout**: `H.timeout * 1000` or 30000ms (30s default)
- **Tools**: `f.options.tools` are passed but model has `toolChoice: undefined`
  and `isNonInteractiveSession: true` — effectively no tool use
- **Query source**: `"hook_prompt"`

### Return Values

| Condition          | Outcome                                                  |
| ------------------ | -------------------------------------------------------- |
| `ok: true`         | `"success"` — hook passes                                |
| `ok: false`        | `"blocking"` — blocks the action, reason shown to Claude |
| Parse/schema error | `"non_blocking_error"` — logged but doesn't block        |
| Timeout/abort      | `"cancelled"`                                            |

---

## Agent Hook (`type: "agent"`)

### Execution Function

Minified name: `otf`
Module: `atf` (hook agent runner)

### What It Does

A **multi-turn agent** with tool access that can inspect the codebase, read the
conversation transcript, and run up to 50 turns before returning a structured
`{ok, reason}` result.

### System Prompt (verbatim from binary)

```
You are verifying a stop condition in Claude Code. Your task is to verify that
the agent completed the given plan. The conversation transcript is available at:
${transcriptPath}
You can read this file to analyze the conversation history if needed.
Use the available tools to inspect the codebase and verify the condition.
Use as few steps as possible - be efficient and direct.
When done, return your result using the ${StructuredOutput} tool with:
- ok: false with reason if the condition is not met
```

Where:

- `${transcriptPath}` = `f.agentId ? OX(f.agentId) : zK()` — the transcript
  JSONL file path for the current agent or session
- `${StructuredOutput}` = the name of the `StructuredOutput` tool (variable `zY`)

### Messages Sent to the LLM

1. **System prompt**: The verification prompt above (with transcript path filled in)
2. **User message**: The hook's `prompt` field with `$ARGUMENTS` substituted
3. **User/system context**: Empty (`userContext: {}, systemContext: {}`)

**The agent does NOT receive the conversation transcript in its messages.** It
receives the _path_ to the transcript file and must use the Read tool to access
it if needed.

### Available Tools

The agent gets **all tools** from the parent context, with two exceptions:

- The `StructuredOutput` tool (variable `zY`) is removed and replaced with a
  custom version (`Qtf()`) that returns `{ok: boolean, reason?: string}`
- Tools in the `QXH` set (disallowed hook tools) are removed

Plus the custom `StructuredOutput` tool which must be called exactly once to
return the verdict.

### Model & Configuration

- **Model**: `H.model ?? Pw()` — hook's configured model or default small model
  (Haiku)
- **Thinking**: Disabled
- **Permission mode**: `"dontAsk"` — all tool calls auto-approved
- **Session rules**: Parent session's allowed rules + `Read(/${transcriptPath})`
  auto-allowed
- **Max turns**: 50 (hardcoded, variable `p`)
- **Timeout**: `H.timeout * 1000` or 60000ms (60s default)
- **Query source**: `"hook_agent"`
- **Agent ID**: Fresh UUID prefixed with `hook-agent-`

### The StructuredOutput Tool

Created by function `Qtf()`:

```typescript
{
  name: "StructuredOutput",  // variable zY
  inputSchema: {
    type: "object",
    properties: {
      ok: { type: "boolean", description: "Whether the condition was met" },
      reason: { type: "string", description: "Reason, if the condition was not met" }
    },
    required: ["ok"],
    additionalProperties: false
  },
  prompt: "Use this tool to return your verification result. You MUST call this tool exactly once at the end of your response."
}
```

### Stop Condition Enforcement

Function `Qu$` registers a "forced stop" handler: if the agent tries to stop
without calling `StructuredOutput`, it's re-prompted with:

> You MUST call the ${StructuredOutput} tool to complete this request. Call this tool now.

This has a 5000ms timeout.

### Return Values

| Condition            | Outcome                          |
| -------------------- | -------------------------------- |
| `ok: true`           | `"success"` — hook passes        |
| `ok: false`          | `"blocking"` — blocks the action |
| 50 turns exceeded    | `"cancelled"`                    |
| No structured output | `"cancelled"`                    |
| Error                | `"non_blocking_error"`           |

---

## Key Differences: Prompt vs Agent

| Aspect               | Prompt Hook        | Agent Hook                          |
| -------------------- | ------------------ | ----------------------------------- |
| **LLM turns**        | 1 (single-turn)    | Up to 50                            |
| **Tool access**      | None               | Full (Read, Grep, Glob, Bash, etc.) |
| **Sees transcript**  | No                 | No (but gets path, can Read it)     |
| **Default timeout**  | 30s                | 60s                                 |
| **Default model**    | Haiku (`Pw()`)     | Haiku (`Pw()`)                      |
| **Permission mode**  | Inherits parent    | `dontAsk` (auto-approve all)        |
| **Token cost**       | ~2,000-5,000       | ~2,000-50,000+                      |
| **Latency**          | ~1-3s              | ~5-30s                              |
| **Output mechanism** | JSON text response | `StructuredOutput` tool call        |

## Command Hook Input (for comparison)

`command` hooks receive the hook input JSON on **stdin** and write their response
JSON to **stdout**. The input is the same JSON that `$ARGUMENTS` expands to for
prompt/agent hooks.

Execution function: `ru$` (deobfuscated: `executeCommandHook`)

The command hook spawns a shell process with these env vars:

- `CLAUDE_PROJECT_DIR`: workspace root
- Standard process env (with sensitive keys scrubbed)

## Function Reference

| Minified | Purpose                                 | Module       |
| -------- | --------------------------------------- | ------------ |
| `itf`    | Execute prompt hook                     | `rtf`        |
| `otf`    | Execute agent hook                      | `atf`        |
| `ru$`    | Execute command hook                    | `Qk$`        |
| `dcA`    | Execute HTTP hook                       | `Lsf`        |
| `cu$`    | Substitute `$ARGUMENTS` in prompt       | `lu$`        |
| `VEH`    | Replace arguments in string             | (shared)     |
| `Qtf`    | Create StructuredOutput tool            | `lu$`        |
| `Qu$`    | Register forced-stop handler            | `lu$`        |
| `y4`     | Create base hook input                  | `Qk$`        |
| `Ksf`    | Parse hook JSON output                  | `Qk$`        |
| `FcA`    | Process parsed hook result              | `Qk$`        |
| `Pw`     | Get default small/fast model            | (config)     |
| `NrH`    | Hook result Zod schema (`{ok, reason}`) | `lu$`        |
| `Mn`     | Single-turn LLM query                   | (query)      |
| `hh`     | Multi-turn agent loop                   | (agent loop) |
| `OX`     | Get transcript path for agent ID        | (transcript) |
| `zK`     | Get session transcript path             | (transcript) |
| `zY`     | StructuredOutput tool name constant     | (tools)      |
| `QXH`    | Set of disallowed hook tool names       | (hooks)      |

## External Resources

See <cli/external_re_resources.md> for public reverse engineering efforts of the
Claude Code CLI binary.
