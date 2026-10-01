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
const eventPage: SessionEventPage = {
  data: [
    {
      event_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
      sequence_num: "1",
      event_type: "user_message",
      source: "client",
      created_at: "2026-09-30T18:40:00Z",
      received_at: null,
      processing_at: null,
      processed_at: null,
      device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
      sent_by_account_id: null,
      payload: {
        content: [
          {
            type: "text",
            text: "Can you make the session browser easier to scan and keep the selected transcript readable?",
          },
        ],
      },
    },
    {
      event_id: "e5d9c30b-5a2e-4b33-9c4d-84a732fab612",
      sequence_num: "2",
      event_type: "assistant_message",
      source: "server",
      created_at: "2026-09-30T18:42:00Z",
      received_at: "2026-09-30T18:42:00Z",
      processing_at: null,
      processed_at: null,
      device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
      sent_by_account_id: null,
      payload: {
        content: [
          {
            type: "text",
            text: "I’ll keep the session list compact, make the active selection clear, and give each transcript event enough room to read.",
          },
        ],
      },
    },
    {
      event_id: "f6ea041c-6b3f-4c44-ad5e-95b8430bc723",
      sequence_num: "3",
      event_type: "tool_result",
      source: "server",
      created_at: "2026-09-30T18:42:30Z",
      received_at: "2026-09-30T18:42:30Z",
      processing_at: null,
      processed_at: null,
      device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
      sent_by_account_id: null,
      payload: {
        tool_name: "Read",
        content: [{ type: "text", text: "The session sync is current." }],
        file_path: "devinfra/claude/session_export/frontend/app.tsx",
      },
    },
  ],
  has_more: false,
  first_id: "d4c8b29a-4f1d-4a22-8b3c-73f621e9a501",
  last_id: "f6ea041c-6b3f-4c44-ad5e-95b8430bc723",
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

createRoot(root).render(
  <MantineProvider defaultColorScheme="auto">
    <App />
  </MantineProvider>
);
