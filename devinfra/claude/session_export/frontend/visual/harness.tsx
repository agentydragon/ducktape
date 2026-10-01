import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import type { SessionEventPage, SessionListPage, SessionSummary } from "../api";
import { SessionViewer } from "../viewer";
import "../page.css";

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
        content: [{ type: "text", text: "The viewer styles are in frontend/page.css." }],
        file_path: "devinfra/claude/session_export/frontend/page.css",
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

  if (url.pathname === "/v1/code/sessions") return Promise.resolve(json(sessionPage));
  if (/^\/v1\/code\/sessions\/[^/]+\/events$/.test(url.pathname)) return Promise.resolve(json(eventPage));
  return Promise.reject(new Error(`Unmocked viewer request: ${url.pathname}`));
}

window.fetch = mockFetch;

const root = document.getElementById("app");
if (!root) throw new Error("Visual test harness is missing #app");

createRoot(root).render(
  <MantineProvider defaultColorScheme="auto">
    <SessionViewer />
  </MantineProvider>
);
