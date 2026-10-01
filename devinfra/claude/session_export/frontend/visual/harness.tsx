import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import type { SessionEventPage, SessionListPage, SessionSummary, SyncStatus } from "../api";
import { App } from "../app";

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

const sessionPage: SessionListPage = { data: sessions, next_cursor: null, resume_token: null };
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
    created_at: `2026-09-30T18:4${sequence}:00Z`,
    received_at: null,
    processing_at: null,
    processed_at: null,
    device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
    sent_by_account_id: null,
    payload,
  };
}

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
        ],
      },
    }),
    fixtureEvent(7, "result", {
      type: "result",
      usage: { total_tokens: 2315 },
      total_cost_usd: 0.0123,
      duration_ms: 2000,
    }),
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a507",
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
  if (url.pathname === "/v1/code/sessions") return Promise.resolve(json(sessionPage));
  if (/^\/v1\/code\/sessions\/[^/]+\/events$/.test(url.pathname)) return Promise.resolve(json(eventPage));
  return Promise.reject(new Error(`Unmocked session sync request: ${url.pathname}`));
}

window.fetch = mockFetch;

const root = document.getElementById("app");
if (!root) throw new Error("Visual test harness is missing #app");
const scenario = new URLSearchParams(window.location.search).get("page") ?? "";
const pathname = scenario.startsWith("SessionSync") ? "/sync" : "/sessions";

createRoot(root).render(
  <MantineProvider defaultColorScheme="auto">
    <App pathname={pathname} />
  </MantineProvider>
);
