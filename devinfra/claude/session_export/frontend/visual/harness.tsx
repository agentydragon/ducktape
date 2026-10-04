import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import type { SessionEventPage, SessionListPage, SessionSummary, SyncStatus } from "../api";
import { App } from "../app";
import {
  longCommandActivityDetail,
  longCommandActivitySessionEvents,
  longCommandActivityTitle,
  noisySession,
  noisySessionEvents,
  narrationSession,
  narrationSessionEvents,
} from "../fixtures/noisy-session";

const FIXED_NOW = Date.parse("2026-09-30T18:45:00Z");
Date.now = () => FIXED_NOW;

const unpairedStatus: SyncStatus = {
  state: "unpaired",
  pairing_started: false,
  credential: null,
  sessions: 2,
  sessions_behind: 0,
  poll_interval_seconds: 300,
  last_cycle: null,
  last_failure: null,
  live: {
    following: false,
    watching: false,
    streams: 0,
    last_event_at: null,
    problems: [],
    failure: null,
  },
};

const pairedStatus: SyncStatus = {
  state: "idle",
  pairing_started: false,
  credential: {
    organization_uuid: "93a9fc36-0c85-49d4-bfa9-0c1ff7c469a1",
    scopes: ["user:profile", "user:sessions:claude_code"],
    access_token_expires_at: "2026-09-30T20:45:00Z",
  },
  sessions: 2,
  sessions_behind: 1,
  poll_interval_seconds: 300,
  last_cycle: { finished_at: "2026-09-30T18:42:00Z", behind: 1, events_read: 3 },
  last_failure: null,
  live: {
    following: true,
    watching: true,
    streams: 1,
    last_event_at: "2026-09-30T18:43:00Z",
    problems: [],
    failure: null,
  },
};

const sessions: Array<SessionSummary & { git_branch: string; repo_path: string }> = [
  {
    id: "session_01a4c9d5-0c2b-4d7e-a2b1-6b8c93ef1201",
    title: "Make the session browser easier to scan",
    status: "active",
    created_at: "2026-09-29T15:10:00Z",
    updated_at: "2026-09-30T18:42:00Z",
    last_event_at: "2026-09-30T18:42:00Z",
    git_branch: "feature/session-viewer",
    repo_path: "~/code/ducktape",
  },
  {
    id: "session_02b5d0e6-1d3c-5e8f-b3c2-7c9d04fa2302",
    title: "Track down a flaky browser test",
    status: "paused",
    created_at: "2026-09-27T09:22:00Z",
    updated_at: "2026-09-29T11:16:00Z",
    last_event_at: "2026-09-29T11:16:00Z",
    git_branch: "debug/browser-test",
    repo_path: "~/code/ducktape",
  },
];

const markdownSession: SessionSummary = {
  id: "session_fixture_markdown",
  title: "Render a readable transcript",
  status: "active",
  created_at: "2026-09-30T18:40:00Z",
  updated_at: "2026-09-30T18:42:00Z",
  last_event_at: "2026-09-30T18:42:00Z",
};

const sessionPage: SessionListPage = { data: sessions, next_cursor: null, resume_token: null };
const sidebarSessions: Array<SessionSummary & { git_branch: string; repo_path: string }> = [
  { ...noisySession, git_branch: "worktree/session-sidebar", repo_path: "~/code/sample-meter" },
  {
    ...noisySession,
    id: "session_fixture_sidebar_2",
    title: "Compare the session list against the transcript",
    status: "active",
    updated_at: "2026-09-29T18:32:00Z",
    last_event_at: "2026-09-29T18:32:00Z",
    git_branch: "feature/session-browser",
    repo_path: "~/code/ducktape",
  },
  {
    ...noisySession,
    id: "session_fixture_sidebar_3",
    title: "Keep a longer session title readable while resizing the sidebar",
    status: "paused",
    updated_at: "2026-09-28T08:12:00Z",
    last_event_at: "2026-09-28T08:12:00Z",
    git_branch: "debug/layout-review",
    repo_path: "~/code/session-tools",
  },
  {
    ...noisySession,
    id: "session_fixture_sidebar_4",
    title: "Check event filtering behavior",
    status: "active",
    updated_at: "2026-09-27T14:04:00Z",
    last_event_at: "2026-09-27T14:04:00Z",
    git_branch: "test/event-filtering",
    repo_path: "~/code/sample-meter",
  },
];
function fixtureEvent(
  sequence: number,
  event_type: string,
  payload: Record<string, unknown>
): SessionEventPage["data"][number] {
  return {
    event_id: `d4c8b29a-4f1d-4a22-8b3c-73f621e9a50${sequence}`,
    sequence_num: String(sequence),
    event_type,
    source: event_type === "user" ? "client" : "server",
    created_at: `2026-09-30T18:${String(sequence).padStart(2, "0")}:00Z`,
    received_at: null,
    processing_at: null,
    processed_at: null,
    device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
    sent_by_account_id: null,
    payload,
  };
}

const markdownEventPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "user", {
      type: "user",
      message: {
        role: "user",
        content: [
          {
            type: "text",
            text: [
              "Could you explain why the value stays **precise** in `parseResult`?",
              "",
              "- Keep all input digits.",
              "- Round only for presentation.",
              "",
              "| Stage | Value |",
              "| --- | ---: |",
              "| Parsed | `12.3456` |",
              "| Display | `12.35` |",
              "",
              "<script>window.__sessionMarkdownFixtureExecuted = true</script>",
              '<img src="x" onerror="window.__sessionMarkdownFixtureExecuted = true">',
              '<a href="javascript:alert(1)" onclick="window.__sessionMarkdownFixtureExecuted = true">unsafe raw link</a>',
              "[unsafe Markdown link](javascript:alert(1))",
              "[unsafe data link](data:text/html,alert(1))",
            ].join("\n"),
          },
        ],
      },
    }),
    fixtureEvent(2, "assistant", {
      type: "assistant",
      message: {
        role: "assistant",
        content: [
          {
            type: "text",
            text: [
              "## Result",
              "",
              "The parser keeps **all four decimal places** until display. See [the synthetic guide](https://example.test/precision).",
              "",
              "Long reference: https://example.test/precision/analysis/parsed-value/retain-every-decimal-place/format-only-at-the-final-display-boundary/line-breaks-must-occur-inside-this-long-address",
              "",
              `Unbroken value: ${"0123456789abcdef".repeat(8)}`,
              "",
              "```ts",
              "export const display = parsed.toFixed(2);",
              "```",
            ].join("\n"),
          },
        ],
      },
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a502",
};

const eventPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "user", {
      type: "user",
      message: { role: "user", content: [{ type: "text", text: "Why is the viewer test failing?" }] },
    }),
    fixtureEvent(2, "assistant", {
      type: "assistant",
      message: {
        role: "assistant",
        content: [
          { type: "thinking", thinking: "I should inspect the failing test and its fixture." },
          { type: "text", text: "I’ll read the test file and follow the fixture." },
          {
            type: "tool_use",
            id: "tool-bash",
            name: "Bash",
            input: { command: "rg -n 'foldSessionEvents' devinfra/claude/session_export/frontend" },
          },
          {
            type: "tool_use",
            id: "tool-grep",
            name: "Grep",
            input: { pattern: "foldSessionEvents", path: "devinfra/claude" },
          },
          {
            type: "tool_use",
            id: "tool-glob",
            name: "Glob",
            input: { pattern: "**/*.test.ts", path: "devinfra/claude" },
          },
          { type: "tool_use", id: "tool-read", name: "Read", input: { file_path: "tests/test_viewer.py" } },
          {
            type: "tool_use",
            id: "tool-agent",
            name: "Task",
            input: { description: "Check the session status fixture" },
          },
        ],
      },
    }),
    fixtureEvent(3, "user", {
      type: "user",
      message: {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "tool-bash",
            content: [{ type: "text", text: "Found the event fold and its tests." }],
          },
          {
            type: "tool_result",
            tool_use_id: "tool-grep",
            content: [{ type: "text", text: "3 matches in the session viewer." }],
          },
          {
            type: "tool_result",
            tool_use_id: "tool-glob",
            content: [{ type: "text", text: "Found 6 frontend test files." }],
          },
          {
            type: "tool_result",
            tool_use_id: "tool-read",
            content: [{ type: "text", text: "The fixture marks the session paused, but the test expected active." }],
          },
          {
            type: "tool_result",
            tool_use_id: "tool-agent",
            content: [{ type: "text", text: "The assertion should expect paused." }],
          },
        ],
      },
    }),
    fixtureEvent(4, "system", {
      type: "system",
      subtype: "task_started",
      task_id: "task-tests",
      parent_tool_use_id: "tool-agent",
      task_type: "agent",
      description: "Check the session status fixture",
    }),
    fixtureEvent(5, "system", {
      type: "system",
      subtype: "task_notification",
      task_id: "task-tests",
      parent_tool_use_id: "tool-agent",
      status: "completed",
      summary: "The fixture uses paused; the assertion expected active.",
    }),
    fixtureEvent(6, "assistant", {
      type: "assistant",
      message: {
        role: "assistant",
        content: [
          {
            type: "text",
            text: "The fixture and assertion disagree. I’ll update the expectation to match the intended paused state.",
          },
          {
            type: "tool_use",
            id: "tool-image",
            name: "Read",
            input: { file_path: "docs/session-sync-flow.png" },
          },
        ],
      },
    }),
    fixtureEvent(7, "user", {
      type: "user",
      tool_use_result: {
        tool_use_id: "tool-image",
        structuredContent: { kind: "synthetic-image-fixture" },
      },
      message: {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "tool-image",
            content: [
              {
                type: "image",
                source: {
                  type: "base64",
                  media_type: "image/png",
                  data: "iVBORw0KGgoAAAANSUhEUgAAAHgAAAA8CAYAAACtrX6oAAAAt0lEQVR4nO3RoRECAQADwa8LWkPRL/Kx0AFI5sKKcxGZ2eM8z5d2O359QIAFWID/NMDjAR4P8HhfgW+Xx1TX+3MqwIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMM+PNA7QCPB3g8wOMBHg/weIDHAzzeGwqER1q6RCsJAAAAAElFTkSuQmCC",
                },
              },
            ],
          },
        ],
      },
    }),
    fixtureEvent(8, "result", {
      type: "result",
      usage: { total_tokens: 2315 },
      total_cost_usd: 0.0123,
      duration_ms: 2000,
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a508",
};

const latestFirstPageEvents = Array.from({ length: 12 }, (_, index) => {
  const sequence = index + 6;
  if (sequence === 10) {
    return fixtureEvent(sequence, "assistant", {
      type: "assistant",
      message: {
        role: "assistant",
        content: [
          { type: "thinking", thinking: "I will keep this disclosure open while older history is prepended." },
          { type: "text", text: "The transcript stays chronological as older events load above." },
        ],
      },
    });
  }
  return fixtureEvent(sequence, sequence % 2 === 0 ? "user" : "assistant", {
    type: sequence % 2 === 0 ? "user" : "assistant",
    message: {
      role: sequence % 2 === 0 ? "user" : "assistant",
      // Keep this fixture scrollable even with compact rows and a small page header.
      content: [
        {
          type: "text",
          text: `Newest-first fixture message ${sequence}; shown in chronological order.\nThis detail keeps the initial history taller than the viewport.\nOlder events must prepend without moving the open disclosure.`,
        },
      ],
    },
  });
});
const latestFirstOlderEvents = Array.from({ length: 5 }, (_, index) => {
  const sequence = index + 1;
  return fixtureEvent(sequence, "user", {
    type: "user",
    message: {
      role: "user",
      content: [{ type: "text", text: `Earlier fixture message ${sequence}.` }],
    },
  });
});
const latestFirstSessionEventPage = (
  descendingEvents: SessionEventPage["data"],
  hasMore: boolean
): SessionEventPage => ({
  data: descendingEvents,
  has_more: hasMore,
  first_id: descendingEvents[0]?.event_id ?? null,
  last_id: descendingEvents.at(-1)?.event_id ?? null,
});

const toolResultEventPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "assistant", {
      type: "assistant",
      message: {
        role: "assistant",
        content: [
          { type: "tool_use", id: "tool-image", name: "Read", input: { file_path: "docs/session-sync-flow.png" } },
        ],
      },
    }),
    fixtureEvent(2, "user", {
      type: "user",
      tool_use_result: { tool_use_id: "tool-image", structuredContent: { kind: "synthetic-image-fixture" } },
      message: {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "tool-image",
            content: [
              { type: "text", text: "A small synthetic diagram is attached." },
              {
                type: "image",
                source: {
                  type: "base64",
                  media_type: "image/png",
                  data: "iVBORw0KGgoAAAANSUhEUgAAAHgAAAA8CAYAAACtrX6oAAAAt0lEQVR4nO3RoRECAQADwa8LWkPRL/Kx0AFI5sKKcxGZ2eM8z5d2O359QIAFWID/NMDjAR4P8HhfgW+Xx1TX+3MqwIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMMGHA5wIABlwMM+PNA7QCPB3g8wOMBHg/weIDHAzzeGwqER1q6RCsJAAAAAElFTkSuQmCC",
                },
              },
            ],
          },
        ],
      },
    }),
    fixtureEvent(3, "result", {
      type: "result",
      usage: { total_tokens: 2315 },
      total_cost_usd: 0.0123,
      duration_ms: 2000,
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a503",
};

const readFileEventPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "assistant", {
      type: "assistant",
      message: {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "tool-read-file",
            name: "Read",
            input: { file_path: "src/session-viewer.ts" },
          },
        ],
      },
    }),
    fixtureEvent(2, "user", {
      type: "user",
      message: {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "tool-read-file",
            content:
              "1: export function compactPreview(value: string): string {\n2:   return value.trim();\n3: }\n\n<system-reminder>fixture-only hidden reminder</system-reminder>",
          },
        ],
      },
    }),
    fixtureEvent(3, "result", {
      type: "result",
      usage: { total_tokens: 815 },
      duration_ms: 200,
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a503",
};

const eventVisibilityPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "user", {
      type: "user",
      message: { content: [{ type: "text", text: "Show me the useful conversation." }] },
    }),
    fixtureEvent(2, "system", {
      type: "system",
      subtype: "hook_started",
      hook_name: "PostToolUse",
      description: "HIDDEN_HOOK_START_DETAIL",
    }),
    fixtureEvent(3, "system", {
      type: "system",
      subtype: "hook_response",
      response: { stdout: "HIDDEN_HOOK_OUTPUT_WITH_A_LARGE_BLOCK" },
    }),
    fixtureEvent(4, "env_manager_log", {
      type: "env_manager_log",
      data: { level: "info", message: "HIDDEN_ENVIRONMENT_MANAGER_LOG" },
    }),
    fixtureEvent(5, "assistant", {
      type: "assistant",
      parent_tool_use_id: "agent-hidden",
      message: {
        content: [{ type: "text", text: "HIDDEN_SUBAGENT_CHILD_MESSAGE" }],
      },
    }),
    fixtureEvent(6, "user", {
      type: "user",
      parent_tool_use_id: "agent-hidden",
      message: {
        content: [{ type: "tool_result", tool_use_id: "child-tool", content: "HIDDEN_CHILD_TOOL_OUTPUT" }],
      },
    }),
    fixtureEvent(7, "assistant", {
      type: "assistant",
      message: { content: [{ type: "text", text: "Hooks and environment logs stay out of this transcript." }] },
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a507",
};

const subagentEventPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "user", {
      type: "user",
      message: { role: "user", content: [{ type: "text", text: "Check why the session status test fails." }] },
    }),
    fixtureEvent(2, "assistant", {
      type: "assistant",
      message: {
        role: "assistant",
        content: [
          { type: "text", text: "I’ll ask an agent to inspect the test and its fixture." },
          {
            type: "tool_use",
            id: "agent-17",
            name: "Task",
            input: { description: "Check the session status test" },
          },
        ],
      },
    }),
    fixtureEvent(3, "assistant", {
      type: "assistant",
      parent_tool_use_id: "agent-17",
      message: {
        role: "assistant",
        model: "claude-sonnet-4-5-20250929",
        content: [
          { type: "tool_use", id: "agent-read", name: "Read", input: { file_path: "tests/test_viewer.py" } },
          { type: "tool_use", id: "agent-grep", name: "Grep", input: { pattern: "status", path: "tests" } },
          { type: "text", text: "The fixture says paused, but the test expects active." },
        ],
      },
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a503",
};

const peerHoldEventPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "system", {
      type: "system",
      subtype: "peer_message_hold",
      message_uuid: "peer-pending-1",
      from: "plan-agent",
      from_name: "Planning agent",
      state: "held",
      cause: "mode-mismatch",
    }),
    fixtureEvent(2, "user", {
      type: "user",
      uuid: "peer-pending-1",
      origin: { kind: "peer", from: "plan-agent", name: "Planning agent" },
      message: { content: [{ type: "text", text: "The fixture uses paused, so the assertion should expect paused." }] },
    }),
    fixtureEvent(3, "user", {
      type: "user",
      uuid: "peer-released-1",
      origin: { kind: "peer", from: "review-agent", name: "Review agent" },
      message: { content: [{ type: "text", text: "I confirmed the cause in the generated manifest." }] },
    }),
    fixtureEvent(4, "system", {
      type: "system",
      subtype: "peer_message_hold",
      message_uuid: "peer-released-1",
      from: "review-agent",
      state: "held",
      cause: "no-mode-asserted",
    }),
    fixtureEvent(5, "system", {
      type: "system",
      subtype: "peer_message_hold",
      message_uuid: "peer-released-1",
      state: "released",
    }),
    fixtureEvent(6, "system", {
      type: "system",
      subtype: "peer_message_hold",
      message_uuid: "peer-dropped-1",
      from: "build-agent",
      from_name: "Build agent",
      state: "dropped",
      outcome: "expired",
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a506",
};

const peerMessageEventPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "user", {
      type: "user",
      uuid: "peer-handback-1",
      origin: {
        kind: "peer",
        from: "review-agent",
        name: "Review agent",
        handback: true,
        handbackNote: "I verified the fixture against the generated manifest and found the mismatch.",
      },
      message: {
        content: [{ type: "text", text: "The manifest is correct; update the test to expect paused." }],
      },
    }),
  ],
  has_more: false,
  first_id: "peer-handback-1",
  last_id: "peer-handback-1",
};

const localCommandEventPage: SessionEventPage = {
  data: [
    fixtureEvent(1, "system", {
      type: "system",
      subtype: "local_command_output",
      content:
        "<local-command-stdout>## Context Usage\n**Model:** claude-sonnet-4-5\n**Tokens:** 42.5k / 200k (21%)\n### Estimated usage by category\n| Category | Tokens |\n| --- | ---: |\n| Messages | 30k |\n| Tools | 12.5k |\n### MCP Tools\n| Tool | Server | Tokens |\n| --- | --- | ---: |\n| search | docs | 2.5k |\n</local-command-stdout>",
    }),
    fixtureEvent(2, "system", {
      type: "system",
      subtype: "local_command_output",
      content:
        '<local-command-stdout><code-stats>{"dailyActivity":[{"date":"2026-09-30","sessionCount":2,"messageCount":12,"toolCallCount":5}]}</code-stats></local-command-stdout>',
    }),
    fixtureEvent(3, "system", {
      type: "system",
      subtype: "local_command_output",
      content: "<local-command-stdout><plan-usage/></local-command-stdout>",
    }),
    fixtureEvent(4, "system", {
      type: "system",
      subtype: "local_command_output",
      content: "<local-command-stdout><session-status/></local-command-stdout>",
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a504",
};

function mockFetch(input: RequestInfo | URL): Promise<Response> {
  const requestUrl = input instanceof Request ? input.url : input instanceof URL ? input.href : input;
  const url = new URL(requestUrl, window.location.href);
  const json = (body: unknown): Response =>
    new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

  if (url.pathname === "/api/status") {
    const page = new URLSearchParams(window.location.search).get("page") ?? "";
    return Promise.resolve(json(page.includes("_paired") ? pairedStatus : unpairedStatus));
  }
  if (url.pathname === "/api/pairing") return Promise.resolve(json({ authorization_url: "https://claude.ai/code" }));
  if (url.pathname === "/api/pairing/complete") return Promise.resolve(json(pairedStatus));
  if (url.pathname === "/api/sync") return Promise.resolve(new Response(null, { status: 202 }));
  if (url.pathname === "/v1/code/sessions") {
    if (scenario.startsWith("SessionMarkdown"))
      return Promise.resolve(json({ data: [markdownSession], next_cursor: null, resume_token: null }));
    return Promise.resolve(
      json(
        scenario.startsWith("SessionNoisySidebar")
          ? { data: sidebarSessions, next_cursor: null, resume_token: null }
          : scenario.startsWith("SessionNarrationVisibility")
            ? {
                data: [
                  { ...narrationSession, git_branch: "feature/narration-fixture", repo_path: "~/code/sample-format" },
                ],
                next_cursor: null,
                resume_token: null,
              }
            : scenario.startsWith("SessionCompletedActivity")
              ? { data: [noisySession], next_cursor: null, resume_token: null }
              : scenario.startsWith("SessionNoisy")
                ? { data: [noisySession], next_cursor: null, resume_token: null }
                : sessionPage
      )
    );
  }
  if (/^\/v1\/code\/sessions\/[^/]+\/events$/.test(url.pathname)) {
    const page = new URLSearchParams(window.location.search).get("page") ?? "";
    if (page.startsWith("SessionMarkdown")) return Promise.resolve(json(markdownEventPage));
    if (page.startsWith("SessionLatestFirst")) {
      const cursor = url.searchParams.get("cursor");
      return Promise.resolve(
        json(
          cursor === latestFirstPageEvents[0]?.event_id
            ? latestFirstSessionEventPage([...latestFirstOlderEvents].reverse(), false)
            : latestFirstSessionEventPage([...latestFirstPageEvents].reverse(), true)
        )
      );
    }
    if (page.startsWith("SessionCompletedActivity")) {
      return Promise.resolve(
        json({
          data: longCommandActivitySessionEvents,
          has_more: false,
          first_id: longCommandActivitySessionEvents[0]?.event_id,
          last_id: longCommandActivitySessionEvents.at(-1)?.event_id,
        })
      );
    }
    if (page.startsWith("SessionNarrationVisibility")) {
      return Promise.resolve(
        json({
          data: narrationSessionEvents,
          has_more: false,
          first_id: narrationSessionEvents[0]?.event_id,
          last_id: narrationSessionEvents.at(-1)?.event_id,
        })
      );
    }
    if (page.startsWith("SessionNoisy"))
      return Promise.resolve(
        json({
          data: noisySessionEvents,
          has_more: false,
          first_id: noisySessionEvents[0]?.event_id,
          last_id: noisySessionEvents.at(-1)?.event_id,
        })
      );
    const events = page.startsWith("SessionReadFileResult")
      ? readFileEventPage
      : page.startsWith("SessionEventVisibility")
        ? eventVisibilityPage
        : page.startsWith("SessionToolResult")
          ? toolResultEventPage
          : page.startsWith("SessionSubagent")
            ? subagentEventPage
            : page.startsWith("SessionPeerMessage")
              ? peerMessageEventPage
              : page.startsWith("SessionPeerHold")
                ? peerHoldEventPage
                : page.startsWith("SessionLocalCommandRows")
                  ? localCommandEventPage
                  : eventPage;
    return Promise.resolve(json(events));
  }
  return Promise.reject(new Error(`Unmocked session sync request: ${url.pathname}`));
}

window.fetch = mockFetch;

const root = document.getElementById("app");
if (!root) throw new Error("Visual test harness is missing #app");
const scenario = new URLSearchParams(window.location.search).get("page") ?? "";
try {
  window.localStorage.removeItem("claude-session-sidebar-visible");
  window.localStorage.removeItem("claude-session-sidebar-width");
} catch {
  // The visual harness starts with its default sidebar state when storage is unavailable.
}
const pathname = scenario.startsWith("SessionSync") ? "/sync" : "/sessions";

function scrollTranscriptElementIntoView(element: HTMLElement): void {
  const viewport = document.querySelector<HTMLDivElement>(
    '[aria-label="Session transcript"] .mantine-ScrollArea-viewport'
  );
  if (viewport === null) return;
  const viewportRect = viewport.getBoundingClientRect();
  const elementRect = element.getBoundingClientRect();
  const targetTop = Math.max(0, (viewport.clientHeight - elementRect.height) / 2);
  viewport.scrollTop += elementRect.top - viewportRect.top - targetTop;
  viewport.dispatchEvent(new Event("scroll"));
}

function clickDisclosureRightEdge(button: HTMLButtonElement): void {
  scrollTranscriptElementIntoView(button);
  const rect = button.getBoundingClientRect();
  const parentRect = button.parentElement!.getBoundingClientRect();
  if (rect.width < parentRect.width - 2) throw new Error("Disclosure does not fill its row");
  if (rect.height > 28) throw new Error("Disclosure exceeds 28px");
  const hit = document.elementFromPoint(rect.right - 2, rect.top + rect.height / 2);
  if (!(hit instanceof HTMLElement) || !button.contains(hit)) {
    throw new Error("Right edge of disclosure is not clickable");
  }
  hit.click();
}

createRoot(root).render(
  <MantineProvider defaultColorScheme="auto">
    <App pathname={pathname} />
  </MantineProvider>
);

if (
  scenario.startsWith("SessionReadFileResult") ||
  scenario.startsWith("SessionToolResult") ||
  scenario.startsWith("SessionSubagent")
) {
  let attempts = 0;
  const expandFixtureDetails = (): void => {
    const toggle = document.querySelector<HTMLButtonElement>("[data-tool-run-toggle]");
    if (toggle !== null) {
      toggle.click();
      const toolRun = toggle.closest<HTMLElement>('[data-fold-kind="tool-run"]');
      if (toolRun !== null) scrollTranscriptElementIntoView(toolRun);
      return;
    }
    attempts += 1;
    if (attempts >= 300) throw new Error("Visual fixture tool disclosure did not mount");
    window.setTimeout(expandFixtureDetails, 20);
  };
  window.setTimeout(expandFixtureDetails, 0);
}

if (scenario.startsWith("SessionCompletedActivity")) {
  let attempts = 0;
  let expanded = false;
  const verifyActivitySummary = (): void => {
    const details = [...document.querySelectorAll<HTMLDetailsElement>('[data-fold-kind="activity"]')].find(
      (candidate) => candidate.querySelector("summary")?.getAttribute("title") === longCommandActivityTitle
    );
    if (details === undefined) {
      attempts += 1;
      if (attempts >= 300) throw new Error("Completed activity fixture did not mount");
      window.setTimeout(verifyActivitySummary, 20);
      return;
    }
    if (!expanded) {
      const closedHeight = details.getBoundingClientRect().height;
      root.dataset.completedActivityClosedHeight = String(closedHeight);
      if (closedHeight > 28) throw new Error(`Collapsed completed activity is ${closedHeight}px tall`);
      if (scenario.includes("Expanded")) {
        expanded = true;
        details.open = true;
        return;
      }
    } else {
      const title = details.querySelector<HTMLElement>("[data-activity-title]");
      const detail = details.querySelector<HTMLElement>("[data-activity-detail]");
      if (
        !details.open ||
        title?.textContent !== longCommandActivityTitle ||
        detail?.textContent !== longCommandActivityDetail
      ) {
        throw new Error("Expanded completed activity omitted its full title or detail");
      }
      if (title.getBoundingClientRect().height <= 0 || detail.getBoundingClientRect().height <= 0) {
        throw new Error("Expanded completed activity title or detail is not visible");
      }
    }
    scrollTranscriptElementIntoView(details);
    root.dataset.completedActivityReady = "true";
    activityObserver.disconnect();
  };
  const activityObserver = new MutationObserver(verifyActivitySummary);
  activityObserver.observe(root, { childList: true, subtree: true, attributes: true });
  window.setTimeout(verifyActivitySummary, 0);
}

if (scenario.startsWith("SessionEventVisibility")) {
  let attempts = 0;
  const assertSuppressedEventContent = (): void => {
    const userMessage = document.querySelector('[data-message-role="user"]');
    const assistantMessage = document.querySelector('[data-message-role="assistant"]');
    if (userMessage !== null && assistantMessage !== null) {
      const transcript = document.querySelector("[data-fold-kind='message']")?.parentElement?.innerText ?? "";
      const hiddenMarkers = [
        "HIDDEN_HOOK_START_DETAIL",
        "HIDDEN_HOOK_OUTPUT_WITH_A_LARGE_BLOCK",
        "HIDDEN_ENVIRONMENT_MANAGER_LOG",
        "HIDDEN_SUBAGENT_CHILD_MESSAGE",
        "HIDDEN_CHILD_TOOL_OUTPUT",
      ];
      const leakedMarker = hiddenMarkers.find((marker) => transcript.includes(marker));
      if (leakedMarker !== undefined)
        throw new Error(`Unsupported event content leaked into transcript: ${leakedMarker}`);
      return;
    }
    attempts += 1;
    if (attempts >= 300) throw new Error("Visible event fixture messages did not mount");
    window.setTimeout(assertSuppressedEventContent, 20);
  };
  window.setTimeout(assertSuppressedEventContent, 0);
}

if (scenario.startsWith("SessionMarkdown")) {
  let attempts = 0;
  const verifyMarkdownSanitization = (): void => {
    const userMarkdown = document.querySelector<HTMLElement>('[data-message-role="user"] .agentplane-markdown');
    const assistantMarkdown = document.querySelector<HTMLElement>(
      '[data-message-role="assistant"] .agentplane-markdown'
    );
    if (userMarkdown === null || assistantMarkdown === null) {
      attempts += 1;
      if (attempts >= 300) throw new Error("Markdown fixture messages did not mount");
      window.setTimeout(verifyMarkdownSanitization, 20);
      return;
    }

    const markdownRoots = [userMarkdown, assistantMarkdown];
    const unsafeElement = markdownRoots.some((markdown) => markdown.querySelector("script, img, [onclick], [onerror]"));
    const unsafeLink = markdownRoots
      .flatMap((markdown) => [...markdown.querySelectorAll<HTMLAnchorElement>("a")])
      .some((link) => /^(javascript|data):/i.test(link.getAttribute("href") ?? ""));
    const fixtureWindow = window as typeof window & { __sessionMarkdownFixtureExecuted?: boolean };
    if (unsafeElement || unsafeLink || fixtureWindow.__sessionMarkdownFixtureExecuted !== undefined)
      throw new Error("Markdown fixture retained executable HTML or an unsafe URL");

    root.dataset.markdownSanitized = "true";
  };
  window.setTimeout(verifyMarkdownSanitization, 0);
}

if (scenario.startsWith("SessionNarrationVisibility")) {
  // Layout is what jsdom cannot show: narration must be laid out, not CSS-hidden.
  const markNarrationVisible = (): void => {
    const narration = document.querySelector<HTMLElement>('[data-fold-kind="narration"]');
    if (narration === null || narration.getBoundingClientRect().height <= 0) return;
    scrollTranscriptElementIntoView(narration);
    root.dataset.narrationReady = "true";
    narrationObserver.disconnect();
  };
  const narrationObserver = new MutationObserver(markNarrationVisible);
  narrationObserver.observe(root, { childList: true, subtree: true, attributes: true });
  window.setTimeout(markNarrationVisible, 0);
}

if (scenario.startsWith("SessionLatestFirst")) {
  let phase: "tail" | "prepended" = "tail";
  let attempts = 0;
  let thinkingDetails: HTMLDetailsElement | null = null;
  let thinkingTop = 0;
  let prependTimeout = 0;
  const failAnchorScenario = (message: string): never => {
    root.dataset.historyAnchorReady = "true";
    root.dataset.historyAnchorError = message;
    throw new Error(message);
  };
  const verifyNewestFirstHistory = (): void => {
    if (phase === "prepended") return;
    const viewport = document.querySelector<HTMLDivElement>(
      '[aria-label="Session transcript"] .mantine-ScrollArea-viewport'
    );
    const latestCard = document.querySelector<HTMLElement>('[data-history-sequences~="17"]');
    if (viewport === null || latestCard === null || viewport.scrollHeight <= viewport.clientHeight) {
      attempts += 1;
      if (attempts >= 300) {
        if (scenario === "SessionLatestFirstAnchor")
          failAnchorScenario("The transcript did not reach its initial tail");
        else throw new Error("The newest-first transcript did not reach its initial tail");
        return;
      }
      window.setTimeout(verifyNewestFirstHistory, 20);
      return;
    }
    const viewportRect = viewport.getBoundingClientRect();
    const latestRect = latestCard.getBoundingClientRect();
    // The latest message can have bottom spacing or an event strip after it.
    // Assert the scroll position and message visibility, not a card-edge coincidence.
    const atTail = Math.abs(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight) <= 1;
    const latestVisible = latestRect.bottom > viewportRect.top && latestRect.top < viewportRect.bottom;
    if (!atTail || !latestVisible) {
      attempts += 1;
      if (attempts >= 300) {
        if (scenario === "SessionLatestFirstAnchor") failAnchorScenario("The newest event is not at the initial tail");
        else throw new Error("The newest event is not visible at the initial transcript tail");
        return;
      }
      window.setTimeout(verifyNewestFirstHistory, 20);
      return;
    }
    root.dataset.latestTailReady = "true";
    if (scenario !== "SessionLatestFirstAnchor") return;

    const details = document.querySelector<HTMLDetailsElement>('details[aria-label="Thinking"]');
    if (details === null) {
      attempts += 1;
      if (attempts >= 300) {
        failAnchorScenario("The native thinking disclosure did not mount");
        return;
      }
      window.setTimeout(verifyNewestFirstHistory, 20);
      return;
    }
    const detailsRect = details.getBoundingClientRect();
    viewport.scrollTop += detailsRect.top - viewportRect.top - 100;
    viewport.dispatchEvent(new Event("scroll"));
    details.open = true;
    window.requestAnimationFrame(() => {
      thinkingDetails = details;
      thinkingTop = details.getBoundingClientRect().top;
      const loadOlder = [...document.querySelectorAll<HTMLButtonElement>("button")].find((button) =>
        button.textContent?.includes("Load older events")
      );
      if (loadOlder === undefined) {
        failAnchorScenario("The older-history control is missing at the initial tail");
        return;
      }
      phase = "prepended";
      loadOlder.click();
      prependTimeout = window.setTimeout(() => {
        failAnchorScenario("The older page did not prepend sequence 1 within 5 seconds");
      }, 5000);
      window.requestAnimationFrame(verifyNewestFirstHistory);
    });
  };
  window.setTimeout(verifyNewestFirstHistory, 0);

  if (scenario === "SessionLatestFirstAnchor") {
    const observer = new MutationObserver(() => {
      if (phase !== "prepended" || !document.querySelector('[data-history-sequences~="1"]')) return;
      window.requestAnimationFrame(() => {
        const viewport = document.querySelector<HTMLDivElement>(
          '[aria-label="Session transcript"] .mantine-ScrollArea-viewport'
        );
        const details = document.querySelector<HTMLDetailsElement>('details[aria-label="Thinking"]');
        const button = [...document.querySelectorAll<HTMLButtonElement>("button")].find((candidate) =>
          candidate.textContent?.includes("Load older events")
        );
        if (viewport === null || details === null || details !== thinkingDetails || !details.open) {
          window.clearTimeout(prependTimeout);
          failAnchorScenario("Prepending older history replaced or closed the open thinking disclosure");
          return;
        }
        if (button !== undefined) {
          window.clearTimeout(prependTimeout);
          failAnchorScenario("The exhausted older-history cursor left the load control enabled");
          return;
        }
        const displacement = Math.abs(details.getBoundingClientRect().top - thinkingTop);
        if (displacement > 1.5) {
          window.clearTimeout(prependTimeout);
          failAnchorScenario(`Prepending older history moved the visible anchor by ${displacement.toFixed(1)}px`);
          return;
        }
        window.clearTimeout(prependTimeout);
        root.dataset.historyAnchorReady = "true";
        observer.disconnect();
      });
    });
    observer.observe(root, { childList: true, subtree: true, attributes: true });
  }
}

if (scenario.startsWith("SessionNoisy")) {
  let openedInspector = false;
  let filtered = false;
  let expanded = false;
  const observer = new MutationObserver(() => {
    if (!document.querySelector('[data-message-role="assistant"]') && !openedInspector) return;
    if (!scenario.includes("Raw") && !scenario.includes("Hook")) {
      if (document.querySelector('[data-fold-kind="notice"]')) throw new Error("Hook noise leaked into the transcript");
      if (scenario.includes("Thinking")) {
        const thinking = document.querySelector<HTMLDetailsElement>('[data-fold-kind="thinking"]');
        if (thinking === null) {
          document.querySelector<HTMLButtonElement>('[data-tool-group-toggle][aria-expanded="false"]')?.click();
          return;
        }
        thinking.open = true;
        scrollTranscriptElementIntoView(thinking);
      }
      root.dataset.noisyReady = "true";
      observer.disconnect();
      return;
    }
    if (!openedInspector) {
      openedInspector = true;
      document.querySelector<HTMLButtonElement>('[aria-label="Show raw event stream"]')?.click();
      return;
    }
    const rawRows = document.querySelectorAll<HTMLDetailsElement>("[data-raw-event]");
    if (rawRows.length === 0) return;
    if (scenario.includes("Hook")) {
      if (!filtered) {
        filtered = true;
        const select = document.querySelector<HTMLSelectElement>('[aria-label="Event kind"]')!;
        select.value = "system · hook_response";
        select.dispatchEvent(new Event("change", { bubbles: true }));
        return;
      }
      if ([...rawRows].some((row) => !row.querySelector("summary")?.textContent?.includes("hook_response"))) return;
      if (!expanded) {
        expanded = true;
        rawRows[0]?.querySelector("summary")?.click();
        if (rawRows[0] !== undefined) scrollTranscriptElementIntoView(rawRows[0]);
        return;
      }
      if (!document.querySelector("[data-event-json]")) return;
    } else if (rawRows.length !== noisySessionEvents.length) {
      throw new Error("The event inspector omitted stored events");
    } else {
      const viewport = document.querySelector<HTMLDivElement>(
        '[aria-label="Session transcript"] .mantine-ScrollArea-viewport'
      );
      if (viewport !== null) {
        viewport.scrollTop = 0;
        viewport.dispatchEvent(new Event("scroll"));
      }
    }
    root.dataset.noisyReady = "true";
    observer.disconnect();
  });
  observer.observe(root, { childList: true, subtree: true, attributes: true });
}

if (scenario.startsWith("SessionNoisySidebar")) {
  const sidebarObserver = new MutationObserver(() => {
    if (root.dataset.noisyReady !== "true") return;

    if (scenario.endsWith("_mobile")) {
      const toggle = document.querySelector<HTMLButtonElement>('button[aria-controls="session-sidebar-mobile"]');
      if (toggle?.getAttribute("aria-expanded") !== "true") {
        toggle?.click();
        return;
      }
      if (document.body.querySelector("#session-sidebar-mobile") === null) return;
      root.dataset.sidebarReady = "mobile-open";
      sidebarObserver.disconnect();
      return;
    }

    if (scenario.includes("Collapsed")) {
      const toggle = document.querySelector<HTMLButtonElement>('button[aria-controls="session-sidebar"]');
      if (toggle?.getAttribute("aria-expanded") !== "false") {
        toggle?.click();
        return;
      }
      root.dataset.sidebarReady = "collapsed";
      sidebarObserver.disconnect();
      return;
    }

    if (scenario.includes("Wide")) {
      const separator = document.querySelector<HTMLElement>("[data-session-sidebar-resizer]");
      if (separator === null) return;
      separator.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
      root.dataset.sidebarReady = "wide";
      sidebarObserver.disconnect();
      return;
    }

    root.dataset.sidebarReady = "expanded";
    sidebarObserver.disconnect();
  });
  sidebarObserver.observe(document.body, { childList: true, subtree: true, attributes: true });
}

if (scenario.startsWith("SessionNoisyTimeline")) {
  let opened = false;
  let expanded = false;
  const observer = new MutationObserver(() => {
    const strips = [...document.querySelectorAll<HTMLElement>("[data-event-strip]")];
    if (strips.length === 0) return;
    const sequences = strips.flatMap((strip) => strip.dataset.historySequences!.split(" "));
    if (sequences.length !== noisySessionEvents.length || new Set(sequences).size !== sequences.length) {
      throw new Error("Timeline must represent every loaded event exactly once");
    }
    const strip = strips[0]!;
    if (!opened) {
      for (const item of strips) {
        if (item.getBoundingClientRect().height > 28) throw new Error("Collapsed event strip exceeds 28px");
        if (item.querySelectorAll("[data-event-dot]").length > 12) throw new Error("Unbounded timeline dot count");
        if (item.scrollWidth > item.clientWidth + 1) throw new Error("Event strip overflows the transcript");
      }
      if (document.querySelector("[data-event-json]")) throw new Error("Timeline expanded JSON by default");
      if (document.querySelectorAll('[data-fold-kind="tool-run"]').length > 3)
        throw new Error("Adjacent tool work was not grouped");
      if (scenario.includes("Expanded")) {
        opened = true;
        strip.querySelector<HTMLButtonElement>("[data-event-dot]")!.click();
        return;
      }
    }
    if (scenario.includes("Expanded")) {
      const detail = strip.querySelector<HTMLDetailsElement>("[data-raw-event]");
      if (detail === null) return;
      if (!expanded) {
        expanded = true;
        detail.querySelector("summary")!.click();
        return;
      }
      if (!strip.querySelector("[data-event-json]")) return;
    }
    scrollTranscriptElementIntoView(strip);
    root.dataset.timelineReady = "true";
    observer.disconnect();
  });
  observer.observe(root, { childList: true, subtree: true, attributes: true });
}

if (scenario.startsWith("SessionNoisyActivity")) {
  let stage = 0;
  const observer = new MutationObserver(() => {
    const group = document.querySelector<HTMLElement>('[data-fold-kind="tool-group"]');
    if (group === null) return;
    if (stage === 0) {
      if (group.getBoundingClientRect().height > 28) throw new Error("Collapsed tool group exceeds 28px");
      if (group.querySelector('[data-fold-kind="tool-run"]')) throw new Error("Tool group expanded by default");
      if (scenario.includes("Expanded")) {
        stage = 1;
        clickDisclosureRightEdge(group.querySelector<HTMLButtonElement>("[data-tool-group-toggle]")!);
        return;
      }
    }
    if (scenario.includes("Expanded")) {
      const tool = group.querySelector<HTMLButtonElement>("[data-tool-run-toggle]");
      if (tool === null) return;
      if (stage === 1) {
        stage = 2;
        clickDisclosureRightEdge(tool);
        return;
      }
      if (!group.querySelector("[data-tool-file-preview]")) return;
    }
    scrollTranscriptElementIntoView(group);
    root.dataset.activityReady = "true";
    observer.disconnect();
  });
  observer.observe(root, { childList: true, subtree: true, attributes: true });
}
