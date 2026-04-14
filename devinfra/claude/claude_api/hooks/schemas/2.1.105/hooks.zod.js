// Extracted from Claude Code npm package v2.1.105
// Source: /tmp/claude-code-2.1.105/package/cli.js (npm package, 14MB minified)
//         Zod namespace: y.  Base schema: g2()
//
// IMPORTANT: All .optional() fields accept undefined (absent) but NOT null.
// Python hooks must use exclude_none=True when serializing output.

import { z } from "zod";

// ============================================================
// Shared permission suggestion types (used in PermissionRequest)
// ============================================================

const PermissionRule = z.object({
  toolName: z.string(),
  ruleContent: z.string().optional(),
});

const PermissionBehavior = z.enum(["allow", "deny", "ask"]);

const PermissionDestination = z.enum(["userSettings", "projectSettings", "localSettings", "session", "cliArg"]);

// Known permission mode values (upstream uses z.string() for the base hook input field,
// but the PermissionSuggestion setMode action uses an explicit enum).
const PermissionMode = z
  .enum(["default", "acceptEdits", "bypassPermissions", "plan", "dontAsk", "auto"])
  .describe(
    "Permission mode for controlling how tool executions are handled. 'default' - Standard behavior. 'acceptEdits' - Auto-accept file edits. 'bypassPermissions' - Bypass all permission checks. 'plan' - Planning mode. 'dontAsk' - Don't prompt for permissions. 'auto' - Auto mode."
  );

const PermissionSuggestion = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("addRules"),
    rules: z.array(PermissionRule),
    behavior: PermissionBehavior,
    destination: PermissionDestination,
  }),
  z.object({
    type: z.literal("replaceRules"),
    rules: z.array(PermissionRule),
    behavior: PermissionBehavior,
    destination: PermissionDestination,
  }),
  z.object({
    type: z.literal("removeRules"),
    rules: z.array(PermissionRule),
    behavior: PermissionBehavior,
    destination: PermissionDestination,
  }),
  z.object({
    type: z.literal("setMode"),
    mode: z.lazy(() => PermissionMode),
    destination: PermissionDestination,
  }),
  z.object({
    type: z.literal("addDirectories"),
    directories: z.array(z.string()),
    destination: PermissionDestination,
  }),
  z.object({
    type: z.literal("removeDirectories"),
    directories: z.array(z.string()),
    destination: PermissionDestination,
  }),
]);

// ============================================================
// Hook Event Names (27 in v2.1.105)
// ============================================================

const HookEventNames = [
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "Notification",
  "UserPromptSubmit",
  "SessionStart",
  "SessionEnd",
  "Stop",
  "StopFailure",
  "SubagentStart",
  "SubagentStop",
  "PreCompact",
  "PostCompact",
  "PermissionRequest",
  "PermissionDenied",
  "Setup",
  "TeammateIdle",
  "TaskCompleted",
  "TaskCreated",
  "Elicitation",
  "ElicitationResult",
  "ConfigChange",
  "InstructionsLoaded",
  "WorktreeCreate",
  "WorktreeRemove",
  "CwdChanged",
  "FileChanged",
];

// ============================================================
// Hook Input Schemas
// ============================================================

// Base fields — present in all hook inputs.
// permission_mode uses z.string() (not z.enum) — may receive future unknown values.
// agent_id and agent_type are in the base schema (optional); narrowed to required in subagent hooks.
const baseHookInput = z.object({
  session_id: z.string(),
  transcript_path: z.string(),
  cwd: z.string(),
  permission_mode: z.string().optional(),
  agent_id: z.string().optional(),
  agent_type: z.string().optional(),
});

// --- Event-specific inputs ---

const PreToolUseInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("PreToolUse"),
    tool_name: z.string(),
    tool_input: z.unknown(),
    tool_use_id: z.string(),
  })
);

const PermissionRequestInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("PermissionRequest"),
    tool_name: z.string(),
    tool_input: z.unknown(),
    permission_suggestions: z.array(PermissionSuggestion).optional(),
  })
);

const PermissionDeniedInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("PermissionDenied"),
    tool_name: z.string(),
    tool_input: z.unknown(),
    tool_use_id: z.string(),
    reason: z.string(),
  })
);

const PostToolUseInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("PostToolUse"),
    tool_name: z.string(),
    tool_input: z.unknown(),
    tool_response: z.unknown(),
    tool_use_id: z.string(),
  })
);

const PostToolUseFailureInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("PostToolUseFailure"),
    tool_name: z.string(),
    tool_input: z.unknown(),
    tool_use_id: z.string(),
    error: z.string(),
    is_interrupt: z.boolean().optional(),
  })
);

const NotificationInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("Notification"),
    message: z.string(),
    title: z.string().optional(),
    notification_type: z.string(),
  })
);

const UserPromptSubmitInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("UserPromptSubmit"),
    prompt: z.string(),
  })
);

const SessionStartInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("SessionStart"),
    source: z.enum(["startup", "resume", "clear", "compact"]),
    agent_type: z.string().optional(),
    model: z.string().optional(),
  })
);

const SetupInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("Setup"),
    trigger: z.enum(["init", "maintenance"]),
  })
);

const StopInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("Stop"),
    stop_hook_active: z.boolean(),
    last_assistant_message: z.string().optional(),
  })
);

const StopFailureInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("StopFailure"),
    stop_hook_active: z.boolean(),
    error: z.string(),
    error_details: z.string().optional(),
    last_assistant_message: z.string().optional(),
  })
);

const SubagentStartInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("SubagentStart"),
    agent_id: z.string(),
    agent_type: z.string(),
  })
);

const SubagentStopInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("SubagentStop"),
    stop_hook_active: z.boolean(),
    agent_id: z.string(),
    agent_transcript_path: z.string(),
    agent_type: z.string(),
    last_assistant_message: z.string().optional(),
  })
);

const PreCompactInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("PreCompact"),
    trigger: z.enum(["manual", "auto"]),
    custom_instructions: z.string().nullable(),
  })
);

const PostCompactInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("PostCompact"),
    trigger: z.enum(["manual", "auto"]),
    compact_summary: z.string(),
  })
);

const TeammateIdleInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("TeammateIdle"),
    teammate_name: z.string(),
    team_name: z.string(),
  })
);

const TaskCompletedInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("TaskCompleted"),
    task_id: z.string(),
    task_subject: z.string(),
    task_description: z.string().optional(),
    teammate_name: z.string().optional(),
    team_name: z.string().optional(),
  })
);

const TaskCreatedInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("TaskCreated"),
    task_id: z.string(),
    task_subject: z.string(),
    task_description: z.string().optional(),
    teammate_name: z.string().optional(),
    team_name: z.string().optional(),
  })
);

const SessionEndReasons = ["clear", "logout", "prompt_input_exit", "other", "bypass_permissions_disabled"];
const SessionEndInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("SessionEnd"),
    reason: z.enum(SessionEndReasons),
  })
);

const ElicitationInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("Elicitation"),
    mcp_server_name: z.string(),
    message: z.string(),
    mode: z.string().optional(),
    url: z.string().optional(),
    elicitation_id: z.string().optional(),
    requested_schema: z.unknown().optional(),
  })
);

// ElicitationResult input: action uses "deny" (not "decline")
// The Elicitation output uses "decline"; ElicitationResult input uses "deny".
const ElicitationResultInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("ElicitationResult"),
    mcp_server_name: z.string(),
    action: z.enum(["accept", "deny", "cancel"]),
    content: z.record(z.string(), z.unknown()).optional(),
    mode: z.string().optional(),
    elicitation_id: z.string().optional(),
  })
);

const ConfigChangeInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("ConfigChange"),
    source: z.string(),
    file_path: z.string().optional(),
  })
);

const InstructionsLoadedInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("InstructionsLoaded"),
    file_path: z.string(),
    memory_type: z.string(),
    load_reason: z.string(),
    globs: z.array(z.string()),
    trigger_file_path: z.string().optional(),
    parent_file_path: z.string().optional(),
  })
);

const WorktreeCreateInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("WorktreeCreate"),
    name: z.string(),
  })
);

const WorktreeRemoveInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("WorktreeRemove"),
    worktree_path: z.string(),
  })
);

const CwdChangedInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("CwdChanged"),
    old_cwd: z.string(),
    new_cwd: z.string(),
  })
);

const FileChangedInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("FileChanged"),
    file_path: z.string(),
    event: z.enum(["change", "add", "unlink"]),
  })
);

const AnyHookInput = z.union([
  PreToolUseInput,
  PostToolUseInput,
  PostToolUseFailureInput,
  NotificationInput,
  UserPromptSubmitInput,
  SessionStartInput,
  SessionEndInput,
  StopInput,
  StopFailureInput,
  SubagentStartInput,
  SubagentStopInput,
  PreCompactInput,
  PostCompactInput,
  PermissionRequestInput,
  PermissionDeniedInput,
  SetupInput,
  TeammateIdleInput,
  TaskCompletedInput,
  TaskCreatedInput,
  ElicitationInput,
  ElicitationResultInput,
  ConfigChangeInput,
  InstructionsLoadedInput,
  WorktreeCreateInput,
  WorktreeRemoveInput,
  CwdChangedInput,
  FileChangedInput,
]);

// ============================================================
// Hook Output Schemas
// ============================================================

const hookOutput = z.object({
  continue: z.boolean().describe("Whether Claude should continue after hook (default: true)").optional(),
  suppressOutput: z.boolean().describe("Hide stdout from transcript (default: false)").optional(),
  stopReason: z.string().describe("Message shown when continue is false").optional(),
  decision: z.enum(["approve", "block"]).optional(),
  reason: z.string().describe("Explanation for the decision").optional(),
  systemMessage: z.string().describe("Warning message shown to the user").optional(),
  hookSpecificOutput: z
    .union([
      z.object({
        hookEventName: z.literal("PreToolUse"),
        permissionDecision: z.enum(["allow", "deny", "ask"]).optional(),
        permissionDecisionReason: z.string().optional(),
        updatedInput: z.record(z.string(), z.unknown()).optional(),
        additionalContext: z.string().optional(),
      }),
      z.object({
        hookEventName: z.literal("UserPromptSubmit"),
        additionalContext: z.string().optional(),
      }),
      z.object({
        hookEventName: z.literal("SessionStart"),
        additionalContext: z.string().optional(),
        initialUserMessage: z.string().optional(),
        watchPaths: z.array(z.string()).optional(),
      }),
      z.object({
        hookEventName: z.literal("Setup"),
        additionalContext: z.string().optional(),
      }),
      z.object({
        hookEventName: z.literal("SubagentStart"),
        additionalContext: z.string().optional(),
      }),
      z.object({
        hookEventName: z.literal("PostToolUse"),
        additionalContext: z.string().optional(),
        updatedMCPToolOutput: z.unknown().describe("Updates the output for MCP tools").optional(),
      }),
      z.object({
        hookEventName: z.literal("PostToolUseFailure"),
        additionalContext: z.string().optional(),
      }),
      z.object({
        hookEventName: z.literal("Notification"),
        additionalContext: z.string().optional(),
      }),
      z.object({
        hookEventName: z.literal("PermissionRequest"),
        decision: z.union([
          z.object({
            behavior: z.literal("allow"),
            updatedInput: z.record(z.string(), z.unknown()).optional(),
            updatedPermissions: z.array(PermissionSuggestion).optional(),
          }),
          z.object({
            behavior: z.literal("deny"),
            message: z.string().optional(),
            interrupt: z.boolean().optional(),
          }),
        ]),
      }),
      z.object({
        hookEventName: z.literal("PermissionDenied"),
        retry: z.boolean().optional(),
      }),
      // Elicitation output: action uses "decline" (hook response to Claude Code)
      z.object({
        hookEventName: z.literal("Elicitation"),
        action: z.enum(["accept", "decline", "cancel"]).optional(),
        content: z.record(z.string(), z.unknown()).optional(),
      }),
      // ElicitationResult output: same enum as Elicitation output
      z.object({
        hookEventName: z.literal("ElicitationResult"),
        action: z.enum(["accept", "decline", "cancel"]).optional(),
        content: z.record(z.string(), z.unknown()).optional(),
      }),
      z.object({
        hookEventName: z.literal("CwdChanged"),
        watchPaths: z.array(z.string()).optional(),
      }),
      z.object({
        hookEventName: z.literal("FileChanged"),
        watchPaths: z.array(z.string()).optional(),
      }),
      z.object({
        hookEventName: z.literal("WorktreeCreate"),
        worktreePath: z.string(),
      }),
    ])
    .optional(),
});

const asyncHookOutput = z.object({
  async: z.literal(true),
  asyncTimeout: z.number().optional(),
});

const hookOutputSchema = z.union([asyncHookOutput, hookOutput]);

export { hookOutput, asyncHookOutput, hookOutputSchema, AnyHookInput };
