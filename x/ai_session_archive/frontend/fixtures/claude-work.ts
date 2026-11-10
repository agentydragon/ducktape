import type { EventRecord } from "../schema";

// Observed Claude tool fields, with synthetic multi-block and mixed-message cases.
const sourceRecords: unknown[] = [
  { type: "user", message: { content: "Inspect the captured README and test results." } },
  { type: "assistant", message: { content: "I'll inspect the captured tool results." } },
  {
    type: "assistant",
    message: {
      content: [
        { type: "thinking", thinking: "Compare the captured README with the failed test output." },
        { type: "tool_use", id: "read-doc", name: "Read", input: { file_path: "/fixture/README.md" } },
        { type: "tool_use", id: "run-check", name: "Bash", input: { command: "check-fixture" } },
      ],
    },
  },
  {
    type: "attachment",
    attachment: { type: "hook_success", hookName: "PostToolUse", stdout: "fixture hook completed", exitCode: 0 },
  },
  {
    type: "user",
    message: {
      content: [
        {
          type: "tool_result",
          tool_use_id: "read-doc",
          content: [{ type: "text", text: "The fixture README documents the required setting." }],
        },
        {
          type: "tool_result",
          tool_use_id: "run-check",
          is_error: true,
          content: "The captured test could not find the required setting.",
        },
      ],
    },
  },
  {
    type: "assistant",
    message: {
      content: [
        { type: "text", text: "The captured check failed. I'll inspect its configuration." },
        { type: "tool_use", id: "read-config", name: "Read", input: { file_path: "/fixture/config.json" } },
      ],
    },
  },
  {
    type: "user",
    message: {
      content: [
        { type: "text", text: "Use the captured data only." },
        {
          type: "tool_result",
          tool_use_id: "read-config",
          content: "The captured configuration has no fixture_enabled entry.",
        },
      ],
    },
  },
  {
    type: "assistant",
    message: {
      content: "The captured results identify a **missing setting**. The transcript contains no successful rerun.",
    },
  },
];

export const claudeWorkRecords: EventRecord[] = sourceRecords.map((parsed, index) => ({
  ordinal: index + 1,
  parsed,
  kind: null,
  timestamp: "2026-10-31T06:00:00Z",
  raw_base64: btoa(JSON.stringify(parsed)),
}));
