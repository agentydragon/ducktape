// Extracted from Claude Code binary v2.1.76
// Source: strings /opt/claude-code/bin/claude, minified names resolved manually
// C. replaced with z. (Zod); UH() is lazy initializer wrapper
//
// IMPORTANT: All .optional() fields accept undefined (absent) but NOT null.
// Python hooks must use exclude_none=True when serializing output.

import { z } from "zod";

// --- PermissionSuggestion (TQH in binary) ---
// A discriminated union of permission modification actions.
// Helper schemas: nuA (rule), luA (behavior), T2H (destination), DHH (permission mode)

const PermissionRule = z.object({
  toolName: z.string(),
  ruleContent: z.string().optional(),
});

const PermissionBehavior = z.enum(["allow", "deny", "ask"]);

const PermissionDestination = z.enum(["userSettings", "projectSettings", "localSettings", "session", "cliArg"]);

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
    mode: z.enum(["default", "acceptEdits", "bypassPermissions", "plan", "dontAsk"]),
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
// Hook Input Schemas
// ============================================================

// --- Base fields (all hook inputs extend this) ---
const baseHookInput = z.object({
  session_id: z.string(),
  transcript_path: z.string(),
  cwd: z.string(),
  permission_mode: z.string().optional(),
  agent_id: z
    .string()
    .optional()
    .describe(
      "Subagent identifier. Present only when the hook fires from within a subagent (e.g., a tool called by an AgentTool worker). Absent for the main thread, even in --agent sessions. Use this field (not agent_type) to distinguish subagent calls from main-thread calls."
    ),
  agent_type: z
    .string()
    .optional()
    .describe(
      'Agent type name (e.g., "general-purpose", "code-reviewer"). Present when the hook fires from within a subagent (alongside agent_id), or on the main thread of a session started with --agent (without agent_id).'
    ),
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
    permission_suggestions: z.array(PermissionSuggestion).optional(), // TQH() in binary
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
    last_assistant_message: z
      .string()
      .optional()
      .describe(
        "Text content of the last assistant message before stopping. Avoids the need to read and parse the transcript file."
      ),
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
    last_assistant_message: z
      .string()
      .optional()
      .describe(
        "Text content of the last assistant message before stopping. Avoids the need to read and parse the transcript file."
      ),
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
    compact_summary: z.string().describe("The conversation summary produced by compaction"),
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

const ElicitationInput = baseHookInput
  .and(
    z.object({
      hook_event_name: z.literal("Elicitation"),
      mcp_server_name: z.string(),
      message: z.string(),
      mode: z.enum(["form", "url"]).optional(),
      url: z.string().optional(),
      elicitation_id: z.string().optional(),
      requested_schema: z.record(z.string(), z.unknown()).optional(),
    })
  )
  .describe(
    "Hook input for the Elicitation event. Fired when an MCP server requests user input. Hooks can auto-respond (accept/decline) instead of showing the dialog."
  );

const ElicitationResultInput = baseHookInput
  .and(
    z.object({
      hook_event_name: z.literal("ElicitationResult"),
      mcp_server_name: z.string(),
      elicitation_id: z.string().optional(),
      mode: z.enum(["form", "url"]).optional(),
      action: z.enum(["accept", "decline", "cancel"]),
      content: z.record(z.string(), z.unknown()).optional(),
    })
  )
  .describe(
    "Hook input for the ElicitationResult event. Fired after the user responds to an MCP elicitation. Hooks can observe or override the response before it is sent to the server."
  );

const ConfigChangeSourceValues = ["user_settings", "project_settings", "local_settings", "policy_settings", "skills"];
const ConfigChangeInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("ConfigChange"),
    source: z.enum(ConfigChangeSourceValues),
    file_path: z.string().optional(),
  })
);

const InstructionsLoadedReasons = ["session_start", "nested_traversal", "path_glob_match", "include"];
const InstructionsLoadedMemoryTypes = ["User", "Project", "Local", "Managed"];
const InstructionsLoadedInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("InstructionsLoaded"),
    file_path: z.string(),
    memory_type: z.enum(InstructionsLoadedMemoryTypes),
    load_reason: z.enum(InstructionsLoadedReasons),
    globs: z.array(z.string()).optional(),
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

const SessionEndReasons = ["clear", "logout", "prompt_input_exit", "other", "bypass_permissions_disabled"];
const SessionEndInput = baseHookInput.and(
  z.object({
    hook_event_name: z.literal("SessionEnd"),
    reason: z.enum(SessionEndReasons),
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
  SubagentStartInput,
  SubagentStopInput,
  PreCompactInput,
  PostCompactInput,
  PermissionRequestInput,
  SetupInput,
  TeammateIdleInput,
  TaskCompletedInput,
  ElicitationInput,
  ElicitationResultInput,
  ConfigChangeInput,
  InstructionsLoadedInput,
  WorktreeCreateInput,
  WorktreeRemoveInput,
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
            updatedPermissions: z.array(PermissionSuggestion).optional(), // qm$() in binary
          }),
          z.object({
            behavior: z.literal("deny"),
            message: z.string().optional(),
            interrupt: z.boolean().optional(),
          }),
        ]),
      }),
      z.object({
        hookEventName: z.literal("Elicitation"),
        action: z.enum(["accept", "decline", "cancel"]).optional(),
        content: z.record(z.string(), z.unknown()).optional(),
      }),
      z.object({
        hookEventName: z.literal("ElicitationResult"),
        action: z.enum(["accept", "decline", "cancel"]).optional(),
        content: z.record(z.string(), z.unknown()).optional(),
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
