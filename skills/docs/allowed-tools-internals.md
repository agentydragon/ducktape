# `allowed-tools` Internals

How the `allowed-tools` frontmatter field in SKILL.md works under the hood,
based on reading the Claude Code source.

## What it does

When a skill is invoked (via the Skill tool), the `allowed-tools` list is
merged into `alwaysAllowRules.command` — the same permission bucket as
user-configured "always allow" rules. This makes listed tools auto-approved
(no per-use prompt) while the skill is active.

## Scope: per-user-message, not per-conversation

The permission grant lasts **for the duration of the agent's response to one
user message** — all tool calls Claude makes before the user types again.

**It does NOT persist across user messages.** Each `submitMessage` call creates
a fresh `processUserInputContext` with the original permission rules.

### Code path

1. `SkillTool.call()` extracts `allowedTools` from the parsed frontmatter
   ([SkillTool.ts:650](https://github.com/anthropics/claude-code))

2. Returns a `contextModifier` that wraps `getAppState()` to inject the tools
   into `alwaysAllowRules.command`:

   ```typescript
   // SkillTool.ts:779-800
   contextModifier(ctx) {
     if (allowedTools.length > 0) {
       modifiedContext = {
         ...ctx,
         getAppState() {
           const appState = previousGetAppState()
           return {
             ...appState,
             toolPermissionContext: {
               ...appState.toolPermissionContext,
               alwaysAllowRules: {
                 ...appState.toolPermissionContext.alwaysAllowRules,
                 command: [...existing, ...allowedTools],
               },
             },
           }
         },
       }
     }
   }
   ```

3. `toolOrchestration.ts` applies the context modifier to `currentContext`,
   which flows into all subsequent tool executions in the same turn:

   ```typescript
   // toolOrchestration.ts:141
   currentContext = update.contextModifier.modifyContext(currentContext);
   ```

4. `query.ts` propagates `updatedToolUseContext` to the next recursive turn
   within the same `queryLoop()` call (line 1717).

5. When the user sends the next message, `QueryEngine.submitMessage()` rebuilds
   `processUserInputContext` from scratch (line 492) — the skill's permission
   override is gone.

### What does NOT reset it

- `addInvokedSkill()` stores the skill content in global state for compaction
  recovery, but this only re-injects the skill's **text content** after
  compaction — it does NOT re-grant `allowedTools` permissions
  (`conversationRecovery.ts` has no `allowedTools`/`alwaysAllow` references).

## Deny rules still win

The permission evaluation order is: **deny > ask > allow**. If a user has a
deny rule for `Bash(rm *)`, a skill's `allowed-tools: Bash` won't override it.
The deny rule takes precedence.

## Source files

All relative to the Claude Code source tree:

| File                                      | Role                                                   |
| ----------------------------------------- | ------------------------------------------------------ |
| `src/tools/SkillTool/SkillTool.ts`        | Parses `allowed-tools`, creates `contextModifier`      |
| `src/services/tools/toolOrchestration.ts` | Applies context modifiers between tool executions      |
| `src/services/tools/toolExecution.ts`     | Attaches `contextModifier` to tool result messages     |
| `src/query.ts`                            | Query loop — propagates modified context across turns  |
| `src/QueryEngine.ts`                      | Rebuilds context per user message (line 492)           |
| `src/utils/permissions/permissions.ts`    | Permission evaluation (deny > ask > allow)             |
| `src/bootstrap/state.ts`                  | `addInvokedSkill` — compaction recovery (content only) |
