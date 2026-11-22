import { clone, create, fromJson, type JsonValue } from "@bufbuild/protobuf";
import { ValueSchema } from "@bufbuild/protobuf/wkt";
import {
  ActionIdentitySchema,
  ActionRequestSchema,
  type ActionRequest,
} from "../../../action_service/api/contracts/action_service_pb";
import type { Route } from "./fetch_mock";

export type ActionKey = { group: string; name: string };
export type ActionArguments = NonNullable<ActionRequest["arguments"]>;

// The visual context freezes the clock before loading the bundle.
const NOW = Date.now();

function ago(ms: number): string {
  return new Date(NOW - ms).toISOString();
}

// What the ssh MCP server's `exec` (ssh_mcp_server/server.py) returns for a listing that names one
// missing path: ls exits 2, and its output runs past the lines the widget shows before "Show all".
export const SSH_EXEC_RESULT = {
  host: "test-archive-host",
  user: "test-user",
  exit_code: 2,
  stdout: [
    "/home/test-user/test-archive:",
    "total 1536",
    ...Array.from({ length: 30 }, (_, index) => {
      const day = index + 1;
      return `-rw-r--r-- 1 test-user test-user 51200 Sep ${String(day).padStart(2)} 03:00 test-backup-2026-09-${String(day).padStart(2, "0")}.tar.zst`;
    }),
    "",
  ].join("\n"),
  stderr: "ls: cannot access '/home/test-user/test-archive/test-missing': No such file or directory\n",
  stdout_truncated: false,
  stderr_truncated: false,
};

export const SSH_PENDING_ACTION = fromJson(ActionRequestSchema, {
  id: "70000000-0000-4000-8000-000000000006",
  // The ssh group in MCP_GROUPS: its `exec` Action has widgets of its own (actions/rendering/ssh/exec.tsx).
  action: { group: "ssh", name: "exec" },
  arguments: {
    host: "test-archive-host",
    user: "test-user",
    command: 'systemctl --user restart test-backup.service && echo "restarted at $(date -Is)"',
    timeout_seconds: 60,
  },
  title: "restart the test backup service",
  description: "Its last run stopped on a stale lock, which a restart clears.",
  idempotencyKey: "visual-ssh-pending",
  caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
  state: "ACTION_STATE_DECISION_PENDING",
  version: "1",
  createdAt: ago(2 * 60_000),
  updatedAt: ago(2 * 60_000),
});

export const SSH_COMPLETED_ACTION = fromJson(ActionRequestSchema, {
  id: "70000000-0000-4000-8000-000000000007",
  action: { group: "ssh", name: "exec" },
  arguments: {
    host: "test-archive-host",
    user: "test-user",
    command: 'ls -l "$HOME/test-archive" "$HOME/test-archive/test-missing"',
  },
  title: "list the test backup archive",
  idempotencyKey: "visual-ssh-completed",
  caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
  state: "ACTION_STATE_SUCCEEDED",
  version: "4",
  createdAt: ago(5 * 60_000),
  updatedAt: ago(4 * 60_000),
  decision: {
    id: "71000000-0000-4000-8000-000000000007",
    verdict: "VERDICT_ALLOW",
    operator: {
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
    },
    idempotencyKey: "visual-allow-ssh",
    decidedAt: ago(4 * 60_000),
  },
  execution: {
    id: "72000000-0000-4000-8000-000000000007",
    state: "EXECUTION_STATE_SUCCEEDED",
    // The whole CallToolResult, as FastMCP answers with the ExecResult: the value as structured
    // content and again as one JSON text block, and the server's info in `_meta`.
    result: {
      _meta: { "io.modelcontextprotocol/serverInfo": { name: "ssh-mcp", version: "4.0.3" } },
      content: [{ type: "text", text: JSON.stringify(SSH_EXEC_RESULT) }],
      structuredContent: SSH_EXEC_RESULT,
      isError: false,
    },
    createdAt: ago(4 * 60_000),
    startedAt: ago(4 * 60_000 - 500),
    completedAt: ago(4 * 60_000 - 1_900),
  },
});

// A single losslessly rendered request, for compact approval screenshots.
export const COMPACT_POD_ACTION = clone(ActionRequestSchema, SSH_PENDING_ACTION);
COMPACT_POD_ACTION.id = "70000000-0000-4000-8000-000000000009";
COMPACT_POD_ACTION.action = create(ActionIdentitySchema, { group: "kubernetes_admin", name: "pods_list_in_namespace" });
COMPACT_POD_ACTION.arguments = {
  namespace: "test-apps",
  labelSelector: "app=demo",
  fieldSelector: "status.phase=Running",
};
COMPACT_POD_ACTION.title = "inspect running demo pods";
COMPACT_POD_ACTION.description = "Read-only inspection of the demo workload.";
COMPACT_POD_ACTION.idempotencyKey = "visual-pods-compact-pending";

export function actionPreview(action: ActionKey, args: ActionArguments): ActionRequest {
  const preview = clone(ActionRequestSchema, COMPACT_POD_ACTION);
  preview.action = create(ActionIdentitySchema, action);
  preview.arguments = args;
  preview.title =
    action.group === "kubernetes_admin" && action.name === "pods_list_in_namespace"
      ? "inspect running demo pods"
      : `review ${action.name.replaceAll("_", " ")}`;
  preview.description = "Check the exact call before approving.";
  return preview;
}

export function actionReceipt(
  template: ActionRequest,
  action: ActionKey,
  args: ActionArguments,
  result: JsonValue
): ActionRequest {
  const receipt = clone(ActionRequestSchema, template);
  receipt.action = create(ActionIdentitySchema, action);
  receipt.arguments = args;
  receipt.title = "Long action text";
  receipt.description = undefined;
  if (receipt.execution === undefined) throw new Error("Receipt fixture has no Execution.");
  receipt.execution.result = fromJson(ValueSchema, result);
  return receipt;
}

export function actionIntegrationSummaryRoute(current: () => Record<string, JsonValue> | null): Route {
  return [
    "GET",
    /^\/upstream\/actions\/v1\/operator\/action-requests\/[^/]+\/(gmail-thread-summaries|gmail-resource-summaries|github-label-summaries|github-push-files-preview|tana-node-summaries|google-calendar-event-preview|grocy-reference-summaries)$/,
    (match) => current()?.[match[1]!],
  ];
}
