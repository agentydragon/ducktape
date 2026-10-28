import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import type { SessionEventPage, SessionListPage, SessionSummary, SyncStatus } from "../api/schema";
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
    return Promise.resolve(json(fixture === "sync-paired" ? pairedStatus : unpairedStatus));
  }
  if (url.pathname === "/api/pairing") return Promise.resolve(json({ authorization_url: "https://claude.ai/code" }));
  if (url.pathname === "/api/pairing/complete") return Promise.resolve(json(pairedStatus));
  if (url.pathname === "/api/sync") return Promise.resolve(new Response(null, { status: 202 }));
  if (url.pathname === "/v1/code/sessions") {
    if (fixture === "markdown")
      return Promise.resolve(json({ data: [markdownSession], next_cursor: null, resume_token: null }));
    return Promise.resolve(
      json(
        fixture === "sidebar"
          ? { data: sidebarSessions, next_cursor: null, resume_token: null }
          : fixture === "narration"
            ? {
                data: [
                  { ...narrationSession, git_branch: "feature/narration-fixture", repo_path: "~/code/sample-format" },
                ],
                next_cursor: null,
                resume_token: null,
              }
            : fixture === "completed-activity"
              ? { data: [noisySession], next_cursor: null, resume_token: null }
              : fixture === "noisy"
                ? { data: [noisySession], next_cursor: null, resume_token: null }
                : sessionPage
      )
    );
  }
  if (/^\/v1\/code\/sessions\/[^/]+\/events$/.test(url.pathname)) {
    if (fixture === "markdown") return Promise.resolve(json(markdownEventPage));
    if (fixture === "history") {
      const cursor = url.searchParams.get("cursor");
      return Promise.resolve(
        json(
          cursor === latestFirstPageEvents[0]?.event_id
            ? latestFirstSessionEventPage([...latestFirstOlderEvents].reverse(), false)
            : latestFirstSessionEventPage([...latestFirstPageEvents].reverse(), true)
        )
      );
    }
    if (fixture === "completed-activity") {
      return Promise.resolve(
        json({
          data: longCommandActivitySessionEvents,
          has_more: false,
          first_id: longCommandActivitySessionEvents[0]?.event_id,
          last_id: longCommandActivitySessionEvents.at(-1)?.event_id,
        })
      );
    }
    if (fixture === "narration") {
      return Promise.resolve(
        json({
          data: narrationSessionEvents,
          has_more: false,
          first_id: narrationSessionEvents[0]?.event_id,
          last_id: narrationSessionEvents.at(-1)?.event_id,
        })
      );
    }
    if (fixture === "noisy" || fixture === "sidebar")
      return Promise.resolve(
        json({
          data: noisySessionEvents,
          has_more: false,
          first_id: noisySessionEvents[0]?.event_id,
          last_id: noisySessionEvents.at(-1)?.event_id,
        })
      );
    const events =
      fixture === "file-result"
        ? readFileEventPage
        : fixture === "tool-result"
          ? toolResultEventPage
          : fixture === "subagent"
            ? subagentEventPage
            : fixture === "peer-message"
              ? peerMessageEventPage
              : fixture === "peer-hold"
                ? peerHoldEventPage
                : fixture === "local-commands"
                  ? localCommandEventPage
                  : eventPage;
    return Promise.resolve(json(events));
  }
  return Promise.reject(new Error(`Unmocked session sync request: ${url.pathname}`));
}

window.fetch = mockFetch;

const root = document.getElementById("app");
if (!root) throw new Error("Visual test harness is missing #app");
const fixture = new URLSearchParams(window.location.search).get("page");
if (
  ![
    "sync-paired",
    "sync",
    "markdown",
    "history",
    "completed-activity",
    "narration",
    "sidebar",
    "noisy",
    "file-result",
    "tool-result",
    "subagent",
    "peer-message",
    "peer-hold",
    "local-commands",
    "viewer",
  ].includes(fixture ?? "")
) {
  throw new Error(`Unknown session fixture ${fixture}`);
}
try {
  window.localStorage.removeItem("claude-session-sidebar-visible");
  window.localStorage.removeItem("claude-session-sidebar-width");
} catch {
  // The visual harness starts with its default sidebar state when storage is unavailable.
}
const pathname = fixture === "sync" || fixture === "sync-paired" ? "/sync" : "/sessions";

// Expected fixture data only. Python owns interactions, layout assertions and capture readiness.
Object.assign(window, {
  __visualFixture__: {
    longCommandActivityTitle,
    longCommandActivityDetail,
    noisyEventCount: noisySessionEvents.length,
  },
});

createRoot(root).render(
  <MantineProvider defaultColorScheme="auto">
    <App pathname={pathname} />
  </MantineProvider>
);
