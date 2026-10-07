/**
 * Visual-test harness: the app mounted on canned data, nothing on the network. The `?page=` query
 * (set by the visual sweep) picks the route; `fetch` (stubbed by network.ts, imported first so the
 * app's client captures the stub) answers API and Electric Shape routes. `EventSource` remains
 * only for the live sandbox, thread, and action-inventory views.
 */
import { electricLive, electricShape, electricSubset, routes, UNANSWERED } from "./network";
import { TEST_REASONING_EFFORTS } from "../test_model_catalog";
import "@mantine/core/styles.css";

import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { createRoot } from "react-dom/client";

import App from "../app";
import { sampleConnection } from "../connections_fixture";
import type { BindingView, Decision, McpLinkageView, PolicyView, SandboxView, ThreadView } from "../client";
import type { ActionGroupView, ActionPolicyView, ActionRequestView } from "../actions/client";
import type { SandboxesSnapshot, SandboxSnapshot, ThreadsSnapshot, WatchHealth } from "../live";
import { EventSchema, ItemKind, RecoveryDisposition, TurnStatus } from "../../../protocol/event_pb";
import { CommandSchema } from "../../../protocol/command_pb";
import { EventEntrySchema } from "../../../protocol/event_log_pb";
import {
  Harness,
  HarnessState,
  SessionSpecSchema,
  SessionSummarySchema,
  type SessionSpec,
  type SessionSummary,
} from "../../../runner/protocol_pb";
import { DisclosureVisual } from "./disclosure_visual";
import { SCENARIOS, type Scenario } from "./scenario";
import { LocalCommands } from "../threads/local_commands";
import { streamRegistry } from "../stream_status";
import { ThemeProvider } from "../theme";

/** Resolved before any fixture is built: the scenario's fields are what the fixtures vary on. */
function resolveScenario(): Scenario {
  const name = new URLSearchParams(window.location.search).get("page") ?? "sandboxes";
  const found: Scenario | undefined = SCENARIOS[name];
  if (found === undefined) throw new Error(`unknown harness scenario ${name}`);
  return found;
}

const scenario = resolveScenario();
// Pages in a visual sweep share an origin. Each scene owns its local-command fixtures.
localStorage.clear();

// The visual sweep freezes the wall clock before this bundle runs, so relative ages stay put.
const NOW = Date.now();
const HOUR = 3_600_000;

function ago(ms: number): string {
  return new Date(NOW - ms).toISOString();
}

const SANDBOXES: SandboxView[] = [
  {
    name: "ready-sandbox",
    uid: "0f9c1d2e-0000-4000-8000-00000000a1b2",
    namespace: "agentplane-visual",
    created_at: ago(3 * HOUR),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "ready-sandbox" },
    status: { conditions: [{ type: "Ready", status: "True", reason: "PodReady", lastTransitionTime: ago(HOUR) }] },
    kubernetes_grants: [
      {
        name: "workspace-read",
        grant: {
          kind: "RoleBinding",
          namespace: "agentplane-visual",
          role_ref: { kind: "Role", name: "workspace-reader" },
        },
      },
    ],
    kubernetes_grants_ready: true,
    kubernetes_grant_error: null,
    launch_grants_pending: false,
    deleting: false,
    pod: {
      name: "ready-sandbox",
      namespace: "agentplane-visual",
      uid: "visual-pod-a1b2",
      deleting: false,
      owner_references: [
        {
          api_version: "agents.x-k8s.io/v1beta1",
          kind: "Sandbox",
          name: "ready-sandbox",
          uid: "0f9c1d2e-0000-4000-8000-00000000a1b2",
          controller: true,
        },
      ],
      node_name: "harness-node",
      status: {
        phase: "Running",
        podIP: "10.0.0.7",
        startTime: ago(2 * HOUR),
        qosClass: "Burstable",
        conditions: [
          { type: "PodScheduled", status: "True", lastTransitionTime: ago(2 * HOUR) },
          { type: "Ready", status: "True", lastTransitionTime: ago(HOUR) },
        ],
        containerStatuses: [
          { name: "runner", state: { running: { startedAt: ago(HOUR) } }, ready: true, restartCount: 0 },
        ],
      },
    },
  },
  {
    name: "pending-sandbox",
    uid: "0f9c1d2e-0000-4000-8000-00000000c3d4",
    namespace: "agentplane-visual",
    created_at: ago(2 * 60_000),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "pending-sandbox" },
    status: { conditions: [{ type: "Ready", status: "False", reason: "PodPending" }] },
    kubernetes_grants: [],
    kubernetes_grants_ready: true,
    kubernetes_grant_error: null,
    launch_grants_pending: false,
    deleting: false,
    pod: {
      name: "pending-sandbox",
      namespace: "agentplane-visual",
      uid: "visual-pod-c3d4",
      deleting: false,
      owner_references: [
        {
          api_version: "agents.x-k8s.io/v1beta1",
          kind: "Sandbox",
          name: "pending-sandbox",
          uid: "0f9c1d2e-0000-4000-8000-00000000c3d4",
          controller: true,
        },
      ],
      node_name: "harness-node",
      status: {
        phase: "Pending",
        conditions: [{ type: "Ready", status: "False", reason: "ContainersNotReady" }],
        containerStatuses: [
          {
            name: "runner",
            state: {
              waiting: {
                reason: "ImagePullBackOff",
                message: 'Back-off pulling image "registry.test/agentplane-runner:harness"',
              },
            },
            ready: false,
            restartCount: 0,
          },
        ],
      },
    },
  },
  {
    name: "suspended-sandbox",
    uid: "0f9c1d2e-0000-4000-8000-00000000e5f6",
    namespace: "agentplane-visual",
    created_at: ago(48 * HOUR),
    operating_mode: "Suspended",
    service_account: { namespace: "agentplane-visual", name: "suspended-sandbox" },
    status: { conditions: [{ type: "Ready", status: "False", reason: "Suspended" }] },
    kubernetes_grants: [],
    kubernetes_grants_ready: true,
    kubernetes_grant_error: null,
    launch_grants_pending: false,
    deleting: false,
    pod: null,
  },
];

if (scenario.grantError) {
  const sandbox = SANDBOXES[0]!;
  sandbox.launch_grants_pending = true;
  sandbox.kubernetes_grants_ready = false;
  sandbox.kubernetes_grant_error = "ApiException (403)";
}

if (scenario.threadlessSandbox) {
  SANDBOXES.push({
    name: "test-provisioning",
    uid: "0f9c1d2e-0000-4000-8000-000000000007",
    namespace: "agentplane-visual",
    created_at: ago(30_000),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "test-provisioning" },
    status: null,
    kubernetes_grants: [],
    kubernetes_grants_ready: true,
    kubernetes_grant_error: null,
    launch_grants_pending: false,
    deleting: false,
    pod: null,
  });
}

const POLICIES: PolicyView[] = [
  {
    name: "github-public",
    rules: [
      {
        hosts: ["api.github.com", "github.com", "*.githubusercontent.com"],
        methods: ["GET", "POST"],
        paths: null,
        credential: {
          name: "harness-github-pat",
          description: "A token for the demo bot account; requests carrying it act as that account.",
          placeholder: "agentplane-credential-harness-github-pat",
          secret: "harness-github-pat",
          key: "token",
          targets: [{ header: "Authorization", method: "schemeToken", scheme: "Bearer" }],
        },
        missing_credential: null,
      },
    ],
  },
  {
    name: "pypi",
    rules: [
      {
        hosts: ["pypi.org", "files.pythonhosted.org"],
        methods: ["GET"],
        paths: ["/simple/**"],
        credential: null,
        missing_credential: null,
      },
    ],
  },
];

/** One seed binding from git, which only git removes; one the app granted at launch, now expired. */
const BINDINGS: BindingView[] = [
  {
    name: "ready-sandbox-7q4xk",
    from_git: false,
    subjects: [{ namespace: "agentplane-visual", name: "ready-sandbox" }],
    expires_at: ago(2 * HOUR),
    policies: [POLICIES[1]],
    missing_policies: [],
  },
  {
    name: "ready-sandbox-github-public",
    from_git: true,
    subjects: [{ namespace: "agentplane-visual", name: "ready-sandbox" }],
    expires_at: null,
    policies: [POLICIES[0]],
    missing_policies: [],
  },
];

/**
 * What the Action Service auto-decides for ready-sandbox: the binding the app wrote at launch, one the
 * operator added for the afternoon, and every state a set can be in -- parsed and judged, edited
 * since it was judged, refused, and missing.
 */
const ACTION_POLICY: ActionPolicyView = {
  synced: true,
  bindings: [
    {
      name: "ready-sandbox-k2m9x",
      provenance: "app",
      expires_at: null,
      ready: { status: "True", reason: "Valid", message: "spec accepted", observed_generation: 1 },
      // The preset names public-coder alone; harness-reviews was picked at launch.
      policy_sets: [
        {
          name: "public-coder",
          generation: 2,
          ready: { status: "True", reason: "Valid", message: "spec accepted", observed_generation: 2 },
          refused: null,
        },
        {
          name: "harness-reviews",
          generation: 1,
          ready: { status: "True", reason: "Valid", message: "spec accepted", observed_generation: 1 },
          refused: null,
        },
      ],
      missing_policy_sets: [],
    },
    {
      name: "ready-sandbox-push-afternoon",
      provenance: "operator",
      expires_at: new Date(NOW + 3 * HOUR).toISOString(),
      ready: null,
      policy_sets: [
        {
          name: "harness-push",
          generation: 3,
          ready: { status: "True", reason: "Valid", message: "spec accepted", observed_generation: 2 },
          refused: null,
        },
        {
          name: "harness-edited",
          generation: 1,
          ready: {
            status: "False",
            reason: "Invalid",
            message: "spec.autoApproveIf.0.type: Input should be 'exact_actions' or 'argument_schema'",
            observed_generation: 1,
          },
          refused: "spec.autoApproveIf.0.type: Input should be 'exact_actions' or 'argument_schema'",
        },
      ],
      missing_policy_sets: ["harness-release"],
    },
  ],
  auto_approve_if: [
    {
      binding: "ready-sandbox-k2m9x",
      policy_set: "public-coder",
      index: 0,
      policy: { type: "exact_actions", actions: { github: ["get_file_contents", "list_commits", "search_code"] } },
    },
    {
      binding: "ready-sandbox-k2m9x",
      policy_set: "public-coder",
      index: 1,
      policy: {
        type: "argument_schema",
        actions: { github: ["create_issue"] },
        argument_schema: {
          properties: { owner: { const: "harness-owner" }, repo: { const: "harness-repo" } },
          required: ["owner", "repo"],
        },
      },
    },
    {
      binding: "ready-sandbox-k2m9x",
      policy_set: "harness-reviews",
      index: 0,
      policy: { type: "exact_actions", actions: { github: ["pull_request_read", "list_pull_requests"] } },
    },
    {
      binding: "ready-sandbox-push-afternoon",
      policy_set: "harness-push",
      index: 0,
      policy: {
        type: "argument_schema",
        actions: { github: ["push_files"] },
        argument_schema: { properties: { branch: { pattern: "^harness/" } }, required: ["branch"] },
      },
    },
  ],
};

const DECISIONS: Decision[] = [
  {
    at: ago(9 * 60_000),
    method: "CONNECT",
    host: "api.github.com",
    port: 443,
    path: null,
    outcome: "allow",
    address: "140.82.116.5",
    reason: null,
    binding: "ready-sandbox-github-public",
    policy: "github-public",
    rule: 0,
    substituted: false,
  },
  {
    at: ago(9 * 60_000 - 200),
    method: "GET",
    host: "api.github.com",
    port: 443,
    path: "/repos/agentydragon/ducktape/pulls",
    outcome: "allow",
    address: "140.82.116.5",
    reason: null,
    binding: "ready-sandbox-github-public",
    policy: "github-public",
    rule: 0,
    substituted: true,
  },
  {
    at: ago(4 * 60_000),
    method: "GET",
    host: "pypi.org",
    port: 443,
    path: "/simple/requests/",
    outcome: "deny",
    address: null,
    reason: "no-rule",
    binding: null,
    policy: null,
    rule: null,
    substituted: false,
  },
  {
    at: ago(60_000),
    method: "CONNECT",
    host: "example.invalid",
    port: 443,
    path: null,
    outcome: "deny",
    address: null,
    reason: "no-binding",
    binding: null,
    policy: null,
    rule: null,
    substituted: false,
  },
];

/** The phone scenario shows the proxy unreachable, the desktop one its decisions; both fit on a page. */
function egressDecisions(): Decision[] | Response {
  if (window.matchMedia("(max-width: 600px)").matches) {
    return Response.json({ detail: "the egress proxy did not answer: connection refused" }, { status: 502 });
  }
  return DECISIONS;
}

const SPEC: SessionSpec = create(SessionSpecSchema, {
  harness: Harness.CLAUDE,
  cwd: "/state/work",
  model: "harness-claude-model",
  reasoningEffort: "low",
});

const SESSIONS: SessionSummary[] = [
  create(SessionSummarySchema, { sessionId: "s-1", spec: SPEC, lastCursor: 14n, harnessState: HarnessState.RUNNING }),
  create(SessionSummarySchema, {
    sessionId: "unnamed-stopped-thread",
    spec: SPEC,
    lastCursor: 31n,
    harnessState: HarnessState.STOPPED,
  }),
];

/** The store's copy of the sessions: s-1 named, the stopped one not, so both renderings are on the page. */
const THREADS: ThreadView[] = [
  {
    id: "5f1c4a2e-0000-4000-8000-000000000001",
    sandbox: "ready-sandbox",
    session_id: "s-1",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(HOUR),
    name: "Idle thread",
    archived: false,
    last_cursor: 14,
    last_event_at: ago(60_000),
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000000",
    sandbox: "ready-sandbox",
    session_id: "unnamed-stopped-thread",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(2 * HOUR),
    name: null,
    archived: false,
    last_cursor: 31,
    last_event_at: ago(90 * 60_000),
    harness_state: "HARNESS_STATE_STOPPED",
    feed_status: "active",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000002",
    sandbox: "ready-sandbox",
    session_id: "s-2",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(30 * 60_000),
    name: "Running thread",
    archived: false,
    last_cursor: 23,
    last_event_at: ago(10_000),
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
    active_turn_id: "t2",
  },
];

/**
 * The sidebar's cross-sandbox fixture: the three THREADS above
 * (all `ready-sandbox`), one each for the pending and suspended sandboxes, one archived, and one whose
 * `sandbox` names no live SandboxView at all -- the struck-through, read-only group.
 */
const THREADS_WITH_SANDBOXES: ThreadView[] = [
  ...THREADS,
  {
    id: "5f1c4a2e-0000-4000-8000-000000000003",
    sandbox: "pending-sandbox",
    session_id: "s-3",
    harness: "HARNESS_CODEX",
    model: "harness-codex-model",
    cwd: "/state/work",
    created_at: ago(5 * 60_000),
    name: "Pending thread",
    archived: false,
    last_cursor: 2,
    last_event_at: ago(5 * 60_000),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000004",
    sandbox: "suspended-sandbox",
    session_id: "s-4",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(47 * HOUR),
    name: "Suspended thread",
    archived: false,
    last_cursor: 9,
    last_event_at: ago(46 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000005",
    sandbox: "deleted-sandbox",
    session_id: "s-5",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(72 * HOUR),
    name: "Read-only thread",
    archived: false,
    last_cursor: 18,
    last_event_at: ago(70 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000006",
    sandbox: "ready-sandbox",
    session_id: "s-6",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(96 * HOUR),
    name: "Archived thread",
    archived: true,
    last_cursor: 3,
    last_event_at: ago(95 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
  },
];

/** The `endedAttachment` scenario: the states thread's harness was shut down, and the runner feed has ended. */
function withEndedAttachment(thread: ThreadView): ThreadView {
  return thread.session_id === "s-2"
    ? {
        ...thread,
        name: "Shut-down thread",
        feed_status: "ended",
        harness_state: "HARNESS_STATE_STOPPED",
        active_turn_id: null,
      }
    : thread;
}

/** The `failedTurn` scenarios: the idle thread's history ends in a failed turn, which is also its last completed one. */
function withFailedTurn(thread: ThreadView): ThreadView {
  return thread.session_id === "s-1"
    ? { ...thread, name: "Failed-turn thread", last_turn_status: "TURN_STATUS_FAILED" }
    : thread;
}

function scenarioThread(thread: ThreadView): ThreadView {
  if (scenario.endedAttachment) return withEndedAttachment(thread);
  return scenario.failedTurn ? withFailedTurn(thread) : thread;
}

// A 32x32 checkerboard, 95 bytes: a real image, small enough to inline.
const DIAGRAM_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgAQMAAABJtOi3AAAABlBMVEX///8ii+b/FUc9AAAAFElEQVR42mNg+A+ERBBEKmOgsnkA7b0/wU6R7xwAAAAASUVORK5CYII=";

// What the ssh MCP server's `exec` (x/ssh_mcp_server/server.py) returns for a listing that names one
// missing path: ls exits 2, and its output runs past the lines the widget shows before "Show all".
const SSH_EXEC_RESULT = {
  host: "test-archive-host",
  user: "test-user",
  exit_code: 2,
  stdout: [
    ...(scenario.hiddenCodepoints
      ? ["review result: visible start \u202Ereversed\u202C, joined\u200Bword, and control\u001Bmarker"]
      : [
          "/home/test-user/test-archive:",
          "total 1536",
          ...Array.from({ length: 30 }, (_, index) => {
            const day = index + 1;
            return `-rw-r--r-- 1 test-user test-user 51200 Sep ${String(day).padStart(2)} 03:00 test-backup-2026-09-${String(day).padStart(2, "0")}.tar.zst`;
          }),
          "",
        ]),
  ].join("\n"),
  stderr: scenario.hiddenCodepoints
    ? "stderr contains a zero-width\u200B separator and U+202E bidi override\u202C\n"
    : "ls: cannot access '/home/test-user/test-archive/test-missing': No such file or directory\n",
  stdout_truncated: false,
  stderr_truncated: false,
};

const ACTIONS: ActionRequestView[] = [
  {
    id: "70000000-0000-4000-8000-000000000001",
    action: { group: "everything", name: "echo" },
    arguments: { repository: "test-owner/test-repository", token: "[redacted]" },
    title: "echo the test repository handle back",
    description: "Confirms the fixture connection still reaches the echo Action before the demo run.",
    idempotency_key: "visual-pending",
    caller: { namespace: "agentplane-visual", name: "test-public-coder" },
    external_grant: {
      caller: { namespace: "agentplane-visual", name: "test-public-coder" },
      issuer: "https://test-actions.example/oauth",
      client_id: "test-external-client",
      connection_id: "73000000-0000-4000-8000-000000000001",
      grant_id: "74000000-0000-4000-8000-000000000001",
      revision: 2,
    },
    state: "decision_pending",
    version: 1,
    created_at: ago(3 * 60_000),
    updated_at: ago(3 * 60_000),
    decision: null,
    execution: null,
  },
  {
    id: "70000000-0000-4000-8000-000000000006",
    // The ssh group in MCP_GROUPS: its `exec` Action has widgets of its own (actions/rendering/ssh.tsx).
    action: { group: "ssh", name: "exec" },
    arguments: {
      host: "test-archive-host",
      user: "test-user",
      command: scenario.longPendingAction
        ? Array.from({ length: 55 }, (_, index) => `echo review-step-${index + 1}`).join("\n")
        : scenario.hiddenCodepoints
          ? 'printf "review \u202Ereversed\u202C zero\u200Bwidth control\u001B"'
          : 'systemctl --user restart test-backup.service && echo "restarted at $(date -Is)"',
      timeout_seconds: 60,
    },
    title: "restart the test backup service",
    description: "Its last run stopped on a stale lock, which a restart clears.",
    idempotency_key: "visual-ssh-pending",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    state: "decision_pending",
    version: 1,
    created_at: ago(2 * 60_000),
    updated_at: ago(2 * 60_000),
    decision: null,
    execution: null,
  },
  {
    id: "70000000-0000-4000-8000-000000000007",
    action: { group: "ssh", name: "exec" },
    arguments: {
      host: "test-archive-host",
      user: "test-user",
      command: 'ls -l "$HOME/test-archive" "$HOME/test-archive/test-missing"',
    },
    title: "list the test backup archive",
    description: null,
    idempotency_key: "visual-ssh-completed",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    state: "succeeded",
    version: 4,
    created_at: ago(5 * 60_000),
    updated_at: ago(4 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000007",
      verdict: "allow",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decision_note: null,
      idempotency_key: "visual-allow-ssh",
      decided_at: ago(4 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000007",
      state: "succeeded",
      // The whole CallToolResult, as FastMCP answers with the ExecResult: the value as structured
      // content and again as one JSON text block, and the server's info in `_meta`.
      result: {
        _meta: { "io.modelcontextprotocol/serverInfo": { name: "ssh-mcp", version: "4.0.3" } },
        content: [{ type: "text", text: JSON.stringify(SSH_EXEC_RESULT) }],
        structuredContent: SSH_EXEC_RESULT,
        isError: false,
      },
      error: null,
      created_at: ago(4 * 60_000),
      started_at: ago(4 * 60_000 - 500),
      completed_at: ago(4 * 60_000 - 1_900),
      reconciled_at: null,
    },
  },
  {
    id: "70000000-0000-4000-8000-000000000002",
    // An MCP group in MCP_GROUPS below, so its stored result is the tool's whole CallToolResult.
    action: { group: "example_docs", name: "render_diagram" },
    arguments: { path: "docs/test-diagram.mmd" },
    title: "render the test diagram",
    description: null,
    idempotency_key: "visual-completed",
    caller: { namespace: "test-agentplane", name: "test-workload" },
    state: "succeeded",
    version: 4,
    created_at: ago(40 * 60_000),
    updated_at: ago(39 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000002",
      verdict: "allow",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decision_note: null,
      idempotency_key: "visual-allow",
      decided_at: ago(39 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000002",
      state: "succeeded",
      result: {
        content: [
          { type: "text", text: "Rendered docs/test-diagram.mmd as a 32x32 PNG." },
          { type: "image", data: DIAGRAM_PNG, mimeType: "image/png" },
          {
            type: "resource_link",
            uri: "https://docs-mcp.example.test/diagrams/test-diagram",
            name: "test-diagram",
            title: "Test diagram page",
          },
        ],
        structuredContent: { path: "docs/test-diagram.mmd", width: 32, height: 32 },
        isError: false,
      },
      error: null,
      created_at: ago(39 * 60_000),
      started_at: ago(39 * 60_000 - 500),
      completed_at: ago(39 * 60_000 - 900),
      reconciled_at: null,
    },
  },
  {
    id: "70000000-0000-4000-8000-000000000003",
    action: { group: "everything", name: "echo" },
    arguments: { repository: "test-owner/other-repository" },
    title: "echo a repository outside the binding",
    description: "Reads test-owner/other-repository, which this workload has no binding for.",
    idempotency_key: "visual-denied",
    caller: { namespace: "agentplane-visual", name: "test-public-coder" },
    external_grant: {
      caller: { namespace: "agentplane-visual", name: "test-public-coder" },
      issuer: "https://test-actions.example/oauth",
      client_id: "test-external-client",
      connection_id: "73000000-0000-4000-8000-000000000003",
      grant_id: "74000000-0000-4000-8000-000000000003",
      revision: 1,
    },
    state: "denied",
    version: 2,
    created_at: ago(80 * 60_000),
    updated_at: ago(79 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000003",
      verdict: "deny",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decision_note: "Out of scope for this workload's binding.",
      idempotency_key: "visual-deny",
      decided_at: ago(79 * 60_000),
    },
    execution: null,
  },
  {
    id: "70000000-0000-4000-8000-000000000004",
    action: { group: "github", name: "search_code" },
    arguments: { repository: "agentydragon/ducktape", query: "auto_allow" },
    title: "search ducktape for auto_allow",
    description: null,
    idempotency_key: "visual-auto-approved",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    state: "succeeded",
    version: 3,
    created_at: ago(15 * 60_000),
    updated_at: ago(14 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000004",
      verdict: "allow",
      provider: "policy_engine",
      operator: null,
      decision_note: null,
      idempotency_key: "visual-policy-allow",
      decided_at: ago(14 * 60_000),
      policy_evidence: {
        bindings: [{ namespace: "agentplane-visual", name: "ready-sandbox-github-public", resource_version: "12345" }],
        policy_sets: [{ namespace: "agentplane-visual", name: "fixture_auto_allow", generation: 1 }],
        matched: {
          namespace: "agentplane-visual",
          policy_set: "fixture_auto_allow",
          source: "autoApproveIf",
          index: 0,
          type: "exact_actions",
        },
      },
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000004",
      state: "succeeded",
      result: { matches: 3 },
      error: null,
      created_at: ago(14 * 60_000),
      started_at: ago(14 * 60_000 - 500),
      completed_at: ago(14 * 60_000 - 900),
      reconciled_at: null,
    },
  },
  {
    id: "70000000-0000-4000-8000-000000000005",
    // A tool's error answer: the Action succeeded, and the CallToolResult says the tool failed.
    action: { group: "example_notes", name: "get_note" },
    arguments: { note_id: "test-missing-note" },
    title: "read the missing test note",
    description: null,
    idempotency_key: "visual-tool-error",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    state: "succeeded",
    version: 4,
    created_at: ago(10 * 60_000),
    updated_at: ago(9 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000005",
      verdict: "allow",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decision_note: null,
      idempotency_key: "visual-allow-tool-error",
      decided_at: ago(9 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000005",
      state: "succeeded",
      result: { content: [{ type: "text", text: "No note has the id test-missing-note." }], isError: true },
      error: null,
      created_at: ago(9 * 60_000),
      started_at: ago(9 * 60_000 - 500),
      completed_at: ago(9 * 60_000 - 900),
      reconciled_at: null,
    },
  },
];

const CONVERSATION_SOURCE = "visual-runner";
const CONVERSATION_EPOCH = "20260921";
const payloadBodies = new Map<string, string>();

function payloadKey(ownerId: string, field: string, generation: string): string {
  return `${ownerId}:${field}:${generation}`;
}

function payload(
  ownerCursor: number,
  ownerId: string,
  field: string,
  body: string,
  revisionCursor = ownerCursor
): Record<string, string> {
  const reference = {
    projection_epoch: CONVERSATION_EPOCH,
    owner_cursor: String(ownerCursor),
    owner_id: ownerId,
    field,
    generation: "1",
    revision_cursor: String(revisionCursor),
    chunk_count: "1",
  };
  payloadBodies.set(payloadKey(ownerId, field, reference.generation), body);
  return reference;
}

function entity(
  kind: "view_state" | "item" | "confirmed_input" | "lifecycle" | "command",
  id: string,
  cursor: number,
  state: Record<string, unknown>,
  extra: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    thread_id: extra.thread_id ?? THREADS[0].id,
    projection_epoch: CONVERSATION_EPOCH,
    entity_kind: kind,
    entity_id: id,
    cursor: String(cursor),
    revision_cursor: String(extra.revision_cursor ?? cursor),
    pending: extra.pending ?? false,
    turn_id: extra.turn_id ?? null,
    state,
    text_ref: extra.text_ref ?? null,
    arguments_ref: extra.arguments_ref ?? null,
    output_ref: extra.output_ref ?? null,
    input_ref: extra.input_ref ?? null,
  };
}

function viewState(
  throughCursor: number,
  activeTurn: string | null,
  operational: Record<string, unknown> = {}
): Record<string, unknown> {
  return entity(
    "view_state",
    "current",
    throughCursor,
    {
      controls: {
        applied_model: "harness-claude-model",
        applied_reasoning_effort: null,
        active_turn_id: activeTurn,
        harness_state: activeTurn === null ? "stopped" : "running",
      },
      operational: {
        status: "active",
        last_verified_cursor: String(throughCursor),
        feed_error: null,
        ...operational,
      },
    },
    { revision_cursor: throughCursor }
  );
}

function lifecycle(
  cursor: number,
  observation: string,
  eventValue: Record<string, unknown>,
  threadId?: string
): Record<string, unknown> {
  return entity(
    "lifecycle",
    `${observation}:${cursor}`,
    cursor,
    { observation, event: eventValue },
    { thread_id: threadId }
  );
}

function item(
  cursor: number,
  id: string,
  kind: ItemKind,
  text: string | null,
  extra: {
    tool?: string;
    arguments?: string;
    output?: string;
    failed?: boolean;
    recovery?: RecoveryDisposition;
    recoveryReason?: string;
    complete?: boolean;
    turn?: string;
    threadId?: string;
  } = {}
): Record<string, unknown> {
  return entity(
    "item",
    id,
    cursor,
    {
      kind,
      tool_name: extra.tool ?? "",
      completion: extra.complete === false ? null : kind === ItemKind.TOOL_CALL ? "tool" : "text",
      tool_succeeded: extra.output === undefined ? null : !extra.failed,
      recovery: extra.recovery ?? null,
      recovery_reason: extra.recoveryReason ?? "",
    },
    {
      thread_id: extra.threadId,
      turn_id: extra.turn ?? "turn-visual",
      text_ref: text === null ? null : payload(cursor, id, "text", text),
      arguments_ref: extra.arguments === undefined ? null : payload(cursor, id, "arguments", extra.arguments),
      output_ref: extra.output === undefined ? null : payload(cursor, id, "output", extra.output),
    }
  );
}

function command(
  cursor: number,
  id: string,
  operation: string,
  outcome: "pending" | "effected" | "failed" | "noop",
  reason: string | null = null,
  text: string | null = null
): Record<string, unknown> {
  return entity(
    "command",
    id,
    cursor,
    { operation, outcome, outcome_cursor: outcome === "pending" ? null : String(cursor), outcome_reason: reason },
    { pending: outcome === "pending", input_ref: text === null ? null : payload(cursor, id, "command_input", text) }
  );
}

const LONG_REASONING_BODY = Array.from(
  { length: 18 },
  (_, index) =>
    `Pass ${index + 1}: I compare the stored body with the event projection, check each boundary for lost formatting, and keep the complete note available as ordinary Markdown. This section is deliberately long to exercise scrolling inside one disclosure.`
).join("\n\n");

function standardRows(
  threadId: string,
  longReasoningPreview: boolean,
  longReasoningBody: boolean
): Record<string, unknown>[] {
  const rows = [
    viewState(34, "turn-visual"),
    entity(
      "confirmed_input",
      "user-1",
      4,
      { harness_message_id: "user-1", origin_command_ids: ["input-1"] },
      {
        thread_id: threadId,
        turn_id: "turn-visual",
        input_ref: payload(4, "user-1", "confirmed_input", "List the repository files."),
      }
    ),
    item(10, "tool-0", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Read",
      arguments: '{"path":"README.md"}',
      output: "# ducktape\n\nRepository instructions are available.",
    }),
    item(
      20,
      "r-1",
      ItemKind.REASONING,
      longReasoningBody
        ? LONG_REASONING_BODY
        : longReasoningPreview
          ? "I will compare the projected row with its source payload. **The streamed body must retain its Markdown formatting** while the one-line preview clips what does not fit. I will verify the fetch path and expanded content before changing behavior. ".repeat(
              2
            )
          : "I will inspect the repository structure, compare **the relevant implementation and tests**, then confirm which path preserves the existing behavior before proposing a change.",
      { threadId }
    ),
    item(28, "m-1", ItemKind.ASSISTANT_TEXT, "I found the project files and the relevant tests.", { threadId }),
    lifecycle(
      31,
      "harness_stderr",
      toJson(
        EventSchema,
        create(EventSchema, { observation: { case: "harnessStderr", value: { text: "warning: fixture stderr" } } })
      ) as Record<string, unknown>,
      threadId
    ),
    item(34, "m-2", ItemKind.ASSISTANT_TEXT, "Next I will read the focused implementation.", {
      threadId,
      complete: false,
    }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function failedRows(threadId: string, afterContent: boolean): Record<string, unknown>[] {
  const error = "Test model request failed: HTTP 429. Quota exhausted for this fixture.";
  const event = toJson(
    EventSchema,
    create(EventSchema, {
      observation: { case: "turnCompleted", value: { turnId: "failed-turn", status: TurnStatus.FAILED, error } },
    })
  ) as Record<string, unknown>;
  const rows = [
    viewState(afterContent ? 8 : 6, null),
    entity(
      "confirmed_input",
      "failed-input",
      4,
      { harness_message_id: "failed-input", origin_command_ids: ["input-failed"] },
      {
        thread_id: threadId,
        turn_id: "failed-turn",
        input_ref: payload(4, "failed-input", "confirmed_input", "Inspect the test repository."),
      }
    ),
    ...(afterContent
      ? [
          item(
            6,
            "partial",
            ItemKind.ASSISTANT_TEXT,
            "The first test files are present. Checking the remaining files…",
            { threadId }
          ),
        ]
      : []),
    lifecycle(afterContent ? 8 : 6, "turn_completed", event, threadId),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function interleavedRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(18, null),
    item(3, "tool-1", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Read",
      arguments: '{"path":"README.md"}',
      output: "README opened",
    }),
    item(
      8,
      "before-input",
      ItemKind.ASSISTANT_TEXT,
      "I checked the current files before processing the queued messages.",
      {
        threadId,
        turn: "interleaved-turn",
      }
    ),
    entity(
      "confirmed_input",
      "coalesced-message",
      10,
      { harness_message_id: "coalesced-message", origin_command_ids: ["input-B", "input-C"] },
      {
        thread_id: threadId,
        turn_id: "interleaved-turn",
        input_ref: payload(10, "coalesced-message", "confirmed_input", "Also inspect tests.\nKeep the patch small."),
      }
    ),
    item(15, "after-input", ItemKind.ASSISTANT_TEXT, "Continuing with the new model…", {
      threadId,
      turn: "interleaved-turn",
    }),
    lifecycle(
      18,
      "turn_completed",
      toJson(
        EventSchema,
        create(EventSchema, {
          observation: {
            case: "turnCompleted",
            value: { turnId: "interleaved-turn", status: TurnStatus.INTERRUPTED, interruptedByCommandId: "interrupt" },
          },
        })
      ) as Record<string, unknown>,
      threadId
    ),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function endedAttachmentRows(threadId: string): Record<string, unknown>[] {
  return interleavedRows(threadId).map((row) => {
    if (row.entity_kind !== "view_state") return row;
    const state = row.state as Record<string, unknown>;
    const operational = state.operational as Record<string, unknown>;
    return { ...row, state: { ...state, operational: { ...operational, status: "ended" } } };
  });
}

/**
 * The statuses the main thread fixture does not produce on its own: a standalone failed tool call, a
 * run whose reasoning is still streaming beside a tool call, and queued commands. Both runs render
 * folded, which is the point: a run's summary is where its streaming and failed indicators show.
 * The run's first step, at cursor 16, is its anchor. The existing nav/header chrome leaves little
 * vertical room at phone width, so the queued-input dot at the bottom falls off the page; the desktop
 * capture is where every state here is visible.
 */
function statesRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(28, "t2"),
    item(7, "tool-0", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: '{"command":"git branch -d stale"}',
      output: "fatal: branch 'stale' not found.",
      failed: true,
    }),
    item(10, "m-0", ItemKind.ASSISTANT_TEXT, "That branch does not exist.", { threadId, turn: "t1" }),
    item(16, "r-0", ItemKind.REASONING, "Running the suite twice exposes flaky failures.", {
      threadId,
      complete: false,
      turn: "t2",
    }),
    item(19, "tool-1", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: '{"command":"bazel test //..."}',
      output: "42 passed",
      turn: "t2",
    }),
    command(
      24,
      "queued-model",
      "change_model",
      scenario.pendingCommands === "outcomes" ? "failed" : "pending",
      "Model unavailable"
    ),
    command(
      25,
      "queued-interrupt",
      "interrupt_turn",
      scenario.pendingCommands === "outcomes" ? "noop" : "pending",
      "Target turn already ended"
    ),
    // Admitted and still pending, so it renders inline as a pending message bubble rather than in
    // the pending-commands box below -- see projected_session.tsx's pendingSentMessage.
    command(26, "queued-submit", "submit_input", "pending", null, "Continue past the failing test once it lands."),
    command(
      27,
      "failed-submit",
      "submit_input",
      "failed",
      "Runner could not start this turn.",
      "Run the rejected input."
    ),
    command(
      28,
      "noop-submit",
      "submit_input",
      "noop",
      "The harness was already stopping.",
      "Run the input that was not applied."
    ),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** Mirror the screenshot's order: assistant text, a folded run, then an unfinished assistant-text
 * item whose streaming badge renders before its body. Copy is synthetic; only the row states matter. */
function streamingInterleavedRows(threadId: string): Record<string, unknown>[] {
  const activeTurn = "streaming-turn";
  let cursor = 1;
  const rows: Record<string, unknown>[] = [];

  const appendAssistantText = (id: string, text: string, streaming: boolean): void => {
    rows.push(
      item(cursor++, id, ItemKind.ASSISTANT_TEXT, text, {
        threadId,
        complete: !streaming,
        turn: activeTurn,
      })
    );
  };

  const appendRun = (prefix: string, toolCalls: number, reasoningSteps: number, failedTool?: number): void => {
    for (let index = 0; index < Math.max(toolCalls, reasoningSteps); index++) {
      if (index < toolCalls) {
        const failed = index === failedTool;
        rows.push(
          item(cursor++, `${prefix}-tool-${index}`, ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Search",
            output: failed ? "The fixture marks this tool call as failed." : undefined,
            failed,
            turn: activeTurn,
          })
        );
      }
      if (index < reasoningSteps)
        rows.push(
          item(cursor++, `${prefix}-reasoning-${index}`, ItemKind.REASONING, "Evaluating the latest results.", {
            threadId,
            turn: activeTurn,
          })
        );
    }
  };

  appendAssistantText(
    "previous-response",
    "Understood — fold it into basic itself, not into default_policies alongside it. That's the better shape: basic is \"what every agent can do\", and the API server is part of that. Let me look at basic's construction and every reference to KUBERNETES_POLICY, because folding it in makes the separate policy a lie unless I delete it too.",
    false
  );
  appendRun("large-run", 32, 17);
  appendAssistantText(
    "streaming-response-1",
    "Understood — folding it into basic rather than defaulting a separate policy. Let me check what asserts on the policy set before I move the rule.",
    true
  );
  appendRun("small-run-1", 2, 1);
  appendAssistantText(
    "streaming-response-2",
    'Understood — that\'s a different and better shape: basic is "the self-identity plumbing every agent needs", and the API server belongs in it. Let me check what merging it would touch, then do it.',
    true
  );
  appendRun("failed-run", 2, 1, 0);
  appendAssistantText(
    "streaming-response-3",
    "Understood — that's a different and better shape: basic is the floor, and Kubernetes reach is part of the floor. Let me check what that implies before editing.",
    true
  );
  appendRun("small-run-2", 3, 2);
  appendAssistantText(
    "streaming-response-4",
    'Understood — the rule belongs in basic, and the separate Kubernetes policy should be removed with it. I\'ll make the change and verify the result.\n\n```python\nanswer = "ready"\nprint(answer)',
    true
  );
  rows.unshift(viewState(cursor - 1, activeTurn));
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** Three mundane observations that collapse into one comma-joined row, then a prominent one
 * (harness lost) that stands alone, then one final mundane one -- lone, so it groups with nothing. */
function lifecycleGroupRows(threadId: string): Record<string, unknown>[] {
  const event = (value: MessageInitShape<typeof EventSchema>["observation"]): Record<string, unknown> =>
    toJson(EventSchema, create(EventSchema, { observation: value })) as Record<string, unknown>;
  const rows = [
    viewState(50, null),
    lifecycle(10, "turn_started", event({ case: "turnStarted", value: { turnId: "turn-visual" } }), threadId),
    lifecycle(20, "harness_started", event({ case: "harnessStarted", value: {} }), threadId),
    lifecycle(
      30,
      "turn_completed",
      event({ case: "turnCompleted", value: { turnId: "turn-visual", status: TurnStatus.COMPLETED } }),
      threadId
    ),
    lifecycle(40, "harness_lost", event({ case: "harnessLost", value: {} }), threadId),
    lifecycle(50, "harness_started", event({ case: "harnessStarted", value: {} }), threadId),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function setupRows(threadId: string): Record<string, unknown>[] {
  const event = (value: MessageInitShape<typeof EventSchema>["observation"]): Record<string, unknown> =>
    toJson(EventSchema, create(EventSchema, { observation: value })) as Record<string, unknown>;
  return [
    { ...viewState(4, null), thread_id: threadId },
    lifecycle(1, "setup_started", event({ case: "setupStarted", value: {} }), threadId),
    lifecycle(
      2,
      "setup_output",
      event({
        case: "setupOutput",
        value: { stream: { case: "stdout", value: new TextEncoder().encode("Workspace ready\n") } },
      }),
      threadId
    ),
    lifecycle(3, "setup_finished", event({ case: "setupFinished", value: { exitCode: 0 } }), threadId),
    lifecycle(4, "harness_started", event({ case: "harnessStarted", value: {} }), threadId),
  ];
}

/** One user turn answered with a fenced Python code block, so Markdown's syntax highlighting of a
 * registered language renders the way tool-call Arguments/Output already do. */
function codeFenceRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(20, null),
    entity(
      "confirmed_input",
      "user-1",
      4,
      { harness_message_id: "user-1", origin_command_ids: ["input-1"] },
      {
        thread_id: threadId,
        turn_id: "turn-visual",
        input_ref: payload(4, "user-1", "confirmed_input", "Add type hints to the greet function."),
      }
    ),
    item(
      20,
      "m-code",
      ItemKind.ASSISTANT_TEXT,
      'Done. The signature now declares its types explicitly:\n\n```python\ndef greet(name: str) -> str:\n    return f"Hello, {name}!"\n```\n',
      { threadId }
    ),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** A reasoning step with no neighboring tool call, so `historyRows` never folds it into a run and
 * `EntityCard` renders it directly -- the standalone case, distinct from `standardRows`'s reasoning
 * step, which sits right after a tool call and so is always part of a run. */
function standaloneReasoningRows(
  threadId: string,
  longPreview: boolean,
  codeFence: boolean,
  longBody: boolean
): Record<string, unknown>[] {
  const reasoning = codeFence
    ? [
        "# Result",
        "",
        "I checked the typed implementation and its returned value:",
        "",
        "```python",
        "def greet(name: str) -> str:",
        '    return f"Hello, {name}!"',
        "```",
        "",
        "The complete implementation remains available when expanded.",
      ].join("\n")
    : longBody
      ? LONG_REASONING_BODY
      : longPreview
        ? "Weighing whether to **add a retry** or fix the root cause first. The latest results point toward the projection path, so I should verify it before changing client behavior."
        : "Weighing which path to try next.";
  const rows = [
    viewState(24, null),
    entity(
      "confirmed_input",
      "user-1",
      4,
      { harness_message_id: "user-1", origin_command_ids: ["input-1"] },
      {
        thread_id: threadId,
        turn_id: "turn-visual",
        input_ref: payload(4, "user-1", "confirmed_input", "What should we try next?"),
      }
    ),
    item(20, "r-solo", ItemKind.REASONING, reasoning, { threadId }),
    item(24, "m-1", ItemKind.ASSISTANT_TEXT, "Let's fix the root cause.", { threadId }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** The command Codex records for a script it ran through a shell: the argv, joined, with the script
 * double-quoted. */
function codexShellCommand(script: string): string {
  return `/bin/bash -lc "${script.replace(/["\\$`]/g, "\\$&")}"`;
}

const LONG_QUERY_SCRIPT = [
  `curl -sS -u 'test-user:test-credential' --data-urlencode "query=WITH latest AS (SELECT DISTINCT ON (account_id, security_id) account_id, security_id, institution_value, captured_at FROM holding_snapshots ORDER BY account_id, security_id, captured_at DESC, id DESC)`,
  `SELECT l.institution_name, a.name AS account, s.name AS security, round(sum(h.institution_value)::numeric, 2) AS market_value FROM latest h JOIN accounts a ON a.account_id = h.account_id JOIN links l ON l.item_id = a.item_id LEFT JOIN securities s ON s.security_id = h.security_id GROUP BY 1, 2, 3 ORDER BY market_value DESC" http://test-pgweb.example/api/query`,
].join(" ");

const LONG_SCRIPT = [
  "set -euo pipefail",
  ...Array.from({ length: 16 }, (_, index) => `echo "step ${index + 1}: $(date -Is)" >> /tmp/test-progress.log`),
  "tail -n 3 /tmp/test-progress.log",
].join("\n");

/** Shell tool calls in the shapes the two harnesses record them: Claude's `Bash` with the model's
 * description, a script past the input's cap, and Codex's joined `bash -lc` command with output past
 * the output's. Neighbouring reasoning keeps them one run. */
function shellCallRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(60, null),
    entity(
      "confirmed_input",
      "user-1",
      4,
      { harness_message_id: "user-1", origin_command_ids: ["input-1"] },
      {
        thread_id: threadId,
        turn_id: "turn-visual",
        input_ref: payload(4, "user-1", "confirmed_input", "Check the containers and the holdings."),
      }
    ),
    item(10, "claude-bash", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: JSON.stringify({
        command: "docker ps --all --format 'table {{.Names}}\\t{{.Status}}'",
        description: "List every container and its status",
      }),
      output: "NAMES\tSTATUS\ntest-web\tUp 3 hours\ntest-db\tUp 3 hours (healthy)",
    }),
    item(20, "r-1", ItemKind.REASONING, "The containers are up, so the holdings query is next.", { threadId }),
    item(30, "codex-command", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "commandExecution",
      arguments: JSON.stringify({ command: codexShellCommand(LONG_QUERY_SCRIPT), cwd: "/test-workspace" }),
      output: JSON.stringify(
        Array.from({ length: 24 }, (_, index) => ({
          institution_name: "Test Bank",
          account: `Test Account ${index + 1}`,
          market_value: (1000 - index * 17.5).toFixed(2),
        })),
        null,
        2
      ),
    }),
    item(40, "claude-script", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: JSON.stringify({ command: LONG_SCRIPT, timeout: 120000 }),
      output: "step 14\nstep 15\nstep 16",
      failed: true,
    }),
    item(50, "claude-streaming", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: '{"command": "bazel test //agentplane/...", "descri',
      complete: false,
    }),
    item(60, "m-1", ItemKind.ASSISTANT_TEXT, "The holdings query returned 24 rows.", { threadId }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** Two reasoning steps with no neighbouring tool call, so each stands alone, both unfinished: one in
 * the turn the harness is running, which is streaming, and one in an earlier turn that ended without
 * finishing it, which is incomplete. */
function unfinishedReasoningRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(30, "turn-visual"),
    item(10, "r-stalled", ItemKind.REASONING, "A step its turn ended without finishing.", {
      threadId,
      complete: false,
      turn: "turn-earlier",
    }),
    item(20, "m-1", ItemKind.ASSISTANT_TEXT, "Between the two steps.", { threadId, turn: "turn-earlier" }),
    item(30, "r-running", ItemKind.REASONING, "A step the running turn is still working through.", {
      threadId,
      complete: false,
    }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** What a recovery leaves on most of a finished turn: every item retained and finished, none badged. */
function quietRecoveryRows(threadId: string): Record<string, unknown>[] {
  const retained = { threadId, recovery: RecoveryDisposition.RETAINED };
  const retainedTool = (cursor: number, id: string) =>
    item(cursor, id, ItemKind.TOOL_CALL, null, {
      ...retained,
      tool: "Retained tool",
      arguments: '{"outcome":"succeeded","context":"retained"}',
      output: "Succeeded and retained: no badge",
    });
  return [
    { ...viewState(50, null), thread_id: threadId },
    retainedTool(10, "retained-tool-1"),
    item(20, "retained-reasoning-in-run", ItemKind.REASONING, "Retained reasoning in a run: no badge", retained),
    retainedTool(30, "retained-tool-2"),
    item(40, "retained-text", ItemKind.ASSISTANT_TEXT, "Retained assistant text: no badge row above it", retained),
    item(50, "retained-reasoning-alone", ItemKind.REASONING, "Retained reasoning on its own: no badge", retained),
  ];
}

function recoveryRows(threadId: string): Record<string, unknown>[] {
  if (scenario.recovery === "quiet") return quietRecoveryRows(threadId);
  const rows =
    scenario.recovery === "tools"
      ? [
          item(10, "revised-tool", ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Bash",
            complete: false,
            recovery: RecoveryDisposition.REVISED,
            arguments: '{"command":"write-report"}',
          }),
          item(20, "discarded-tool", ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Bash",
            output: "Succeeded, then discarded from context",
            recovery: RecoveryDisposition.ABSENT,
          }),
          item(30, "failed-tool", ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Retained tool",
            output: "Failed, still in context",
            failed: true,
            recovery: RecoveryDisposition.RETAINED,
          }),
          item(40, "unknown-tool", ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Bash",
            complete: false,
            recovery: RecoveryDisposition.UNKNOWN,
            recoveryReason: "The harness history could not be inspected.",
          }),
        ]
      : [
          item(10, "retained-text", ItemKind.ASSISTANT_TEXT, "Retained text, interrupted mid-reply", {
            threadId,
            complete: false,
            recovery: RecoveryDisposition.RETAINED,
          }),
          item(20, "discarded-text", ItemKind.ASSISTANT_TEXT, "Remember the name in the margin", {
            threadId,
            complete: false,
            recovery: RecoveryDisposition.ABSENT,
          }),
          item(30, "revised-text", ItemKind.ASSISTANT_TEXT, "This is the text retained for the next turn.", {
            threadId,
            complete: false,
            recovery: RecoveryDisposition.REVISED,
          }),
          item(
            40,
            "unknown-text",
            ItemKind.ASSISTANT_TEXT,
            "Then she heard the bells of her city, ringing under the floor.",
            {
              threadId,
              complete: false,
              recovery: RecoveryDisposition.UNKNOWN,
              recoveryReason: "The harness history could not be inspected.",
            }
          ),
        ];
  if (scenario.recovery === "tools") rows[0].output_ref = payload(10, "revised-tool", "output", "aborted");
  return [{ ...viewState(40, null), thread_id: threadId }, ...rows];
}

function threadEntityRows(threadId: string): Record<string, unknown>[] {
  if (scenario.recovery) return recoveryRows(threadId);
  if (scenario.shellCalls) return shellCallRows(threadId);
  if (scenario.endedAttachment) return endedAttachmentRows(threadId);
  if (scenario.failedTurn) return failedRows(threadId, scenario.failedTurn === "after-content");
  if (scenario.interleavedEvents) return interleavedRows(threadId);
  if (scenario.lifecycleGroup) return lifecycleGroupRows(threadId);
  if (scenario.threadSetup) return setupRows(threadId);
  if (scenario.markdownCodeFence) return codeFenceRows(threadId);
  if (scenario.streamingInterleaved) return streamingInterleavedRows(threadId);
  if (scenario.unfinishedReasoning) return unfinishedReasoningRows(threadId);
  if (scenario.standaloneReasoning)
    return standaloneReasoningRows(
      threadId,
      scenario.longReasoningPreview ?? false,
      scenario.reasoningCodeFence ?? false,
      scenario.longReasoningBody ?? false
    );
  if (threadId === THREADS[2].id || scenario.pendingCommands) return statesRows(threadId);
  return standardRows(threadId, scenario.longReasoningPreview ?? false, scenario.longReasoningBody ?? false);
}

function threadScope(threadId: string): Record<string, string> {
  const through = threadEntityRows(threadId).find((row) => row.entity_kind === "view_state")?.revision_cursor ?? "0";
  return { projection_epoch: CONVERSATION_EPOCH, through_cursor: String(through) };
}

if (scenario.pendingCommands === "mixed") {
  const local = new LocalCommands(THREADS[2].id);
  local.remember(
    create(CommandSchema, {
      commandId: "locally-retained",
      operation: {
        case: "submitInput",
        value: { text: "Continue when ready. This message has no saved confirmation yet." },
      },
    })
  );
}

if (scenario.pendingCommands === "outcomes") {
  // A settled command shows while the browser that sent it still holds it.
  const local = new LocalCommands(THREADS[2].id);
  local.remember(
    create(CommandSchema, {
      commandId: "queued-model",
      operation: { case: "changeModel", value: { model: "next-model" } },
    })
  );
  local.remember(
    create(CommandSchema, {
      commandId: "queued-interrupt",
      operation: { case: "interruptTurn", value: { turnId: "t2" } },
    })
  );
}

// One MCP server per row state the MCP servers page draws: linked and connected, a link whose token
// lapsed while its refresh keeps failing, a refresh the provider refused, never linked, and
// bearer-only backends that are up or unreachable.
const MCP_LINKAGES: McpLinkageView[] = [
  {
    server_id: "example_docs",
    server_url: "https://docs-mcp.example.test/mcp",
    status: "linked",
    revision: 3,
    scopes: ["openid", "offline_access"],
    expires_at: new Date(NOW + HOUR).toISOString(),
    linked_at: ago(30 * 24 * HOUR),
    linked_by: null,
  },
  {
    server_id: "example_cluster",
    server_url: "https://cluster-mcp.example.test/mcp",
    status: "expired",
    revision: 2,
    scopes: ["openid", "email", "profile", "offline_access"],
    expires_at: ago(2 * HOUR),
    linked_at: ago(9 * 24 * HOUR),
    linked_by: null,
    refresh_failure: {
      action: "retrying",
      error: "the token endpoint answered HTTP 503 Service Unavailable",
      attempts: 6,
      retry_at: new Date(NOW + 4 * 60_000).toISOString(),
    },
  },
  {
    server_id: "example_calendar",
    server_url: "https://calendar-mcp.example.test/mcp",
    status: "degraded",
    revision: 5,
    scopes: ["openid", "offline_access"],
    expires_at: ago(HOUR),
    linked_at: ago(40 * 24 * HOUR),
    linked_by: null,
    refresh_failure: {
      action: "reconnect",
      error: "the OAuth provider refused the token request: invalid_grant: Token is not active",
      attempts: 1,
      retry_at: null,
    },
  },
  {
    server_id: "example_pantry",
    server_url: "https://pantry-mcp.example.test/mcp",
    status: "unlinked",
    revision: 0,
    scopes: [],
    expires_at: null,
    linked_at: null,
    linked_by: null,
  },
];

function mcpGroup(key: string, executorDescription: string, health: ActionGroupView["health"]): ActionGroupView {
  return {
    key,
    title: key,
    description: `Test MCP backend ${key}.`,
    executor_kind: "mcp",
    executor_description: executorDescription,
    available: health?.state === "available",
    health,
    actions: [],
  };
}

const MCP_GROUPS: ActionGroupView[] = [
  mcpGroup("example_docs", "Linked operator account.", {
    state: "available",
    reason: null,
    detail: null,
    last_discovery_at: ago(60_000),
    retry_at: null,
    failures: 0,
  }),
  mcpGroup("example_cluster", "Linked operator account.", {
    state: "disconnected",
    reason: "linkage_unavailable",
    detail: `the access token expired at ${ago(2 * HOUR)
      .replace("T", " ")
      .slice(0, 19)} UTC and has not been refreshed`,
    last_discovery_at: ago(3 * HOUR),
    retry_at: new Date(NOW + 20_000).toISOString(),
    failures: 12,
  }),
  mcpGroup("example_calendar", "Linked operator account.", {
    state: "disconnected",
    reason: "linkage_unavailable",
    detail: "refreshing the token failed in a way retrying cannot fix; link the account again",
    last_discovery_at: ago(HOUR),
    retry_at: new Date(NOW + 20_000).toISOString(),
    failures: 9,
  }),
  mcpGroup("example_pantry", "Linked operator account.", {
    state: "disconnected",
    reason: "linkage_unavailable",
    detail: "no account is linked",
    last_discovery_at: null,
    retry_at: new Date(NOW + 20_000).toISOString(),
    failures: 4,
  }),
  mcpGroup("example_mail", "Test MCP backend behind a static bearer.", {
    state: "disconnected",
    reason: "connect_failed",
    detail: "RuntimeError: Client failed to connect: All connection attempts failed",
    last_discovery_at: null,
    retry_at: new Date(NOW + 20_000).toISOString(),
    failures: 7,
  }),
  mcpGroup("example_notes", "Test MCP backend behind a static bearer.", {
    state: "available",
    reason: null,
    detail: null,
    last_discovery_at: ago(60_000),
    retry_at: null,
    failures: 0,
  }),
  mcpGroup("ssh", "Test MCP backend behind a static bearer.", {
    state: "available",
    reason: null,
    detail: null,
    last_discovery_at: ago(60_000),
    retry_at: null,
    failures: 0,
  }),
];

// Only what a page still asks for: the sandboxes, their bindings and their threads arrive on the
// live streams above.
routes.push(
  [
    "GET",
    /^\/models$/,
    () => ({
      models: [
        {
          model: "harness-claude-model",
          display_name: "Harness Claude Model",
          reasoning_efforts: TEST_REASONING_EFFORTS,
        },
        { model: "next-model", display_name: "Next Model", reasoning_efforts: TEST_REASONING_EFFORTS },
        {
          model: "harness-codex-model",
          display_name: "Harness Codex Model",
          reasoning_efforts: TEST_REASONING_EFFORTS,
        },
      ],
      harnesses: {
        HARNESS_CLAUDE: scenario.claudePaused ? [] : ["harness-claude-model", "next-model"],
        HARNESS_CODEX: ["harness-codex-model"],
      },
    }),
  ],
  [
    "GET",
    /^\/presets$/,
    () => [
      {
        name: "public-coder",
        title: "Public coder",
        template: "agentplane-runner",
        egress_policies: ["github-public"],
        action_policy_sets: ["public-coder"],
        kubernetes_grants: ["workspace-read"],
        session_defaults: {
          harness: "HARNESS_CODEX",
          model: "harness-codex-model",
          cwd: "/state/workspaces/{session_id}",
          reasoning_effort: "medium",
          instructions: "Work on public repositories only.",
        },
        bootstrap: "mkdir -p /state/workspaces",
      },
    ],
  ],
  ["GET", /^\/sandboxes\/templates$/, () => ["agentplane-runner"]],
  [
    "GET",
    /^\/kubernetes-grants$/,
    () => [
      {
        name: "workspace-read",
        kind: "RoleBinding",
        namespace: "agentplane-visual",
        role_ref: { kind: "Role", name: "workspace-reader" },
      },
      {
        name: "config-read",
        kind: "RoleBinding",
        namespace: "agentplane-visual",
        role_ref: { kind: "Role", name: "config-reader" },
      },
    ],
  ],
  ["GET", /^\/egress\/policies$/, () => POLICIES],
  ["GET", /^\/action-policy\/sets$/, () => ACTION_POLICY.bindings.flatMap((binding) => binding.policy_sets)],
  ["GET", /^\/actions$/, () => ACTIONS],
  [
    "GET",
    /^\/actions\/history$/,
    (_match, query) => {
      const past = ACTIONS.filter((request) => request.state !== "decision_pending");
      return scenario.historyPaged && !query.has("cursor")
        ? { items: past.slice(0, 2), next_cursor: "second-page" }
        : { items: scenario.historyPaged ? past.slice(2) : past, next_cursor: null };
    },
  ],
  [
    "GET",
    /^\/connections$/,
    () => [
      sampleConnection(),
      {
        ...sampleConnection(),
        id: "10000000-0000-4000-8000-000000000002",
        display_name: "Retired research client",
        grants: sampleConnection().grants.map((grant) => ({
          ...grant,
          id: "20000000-0000-4000-8000-000000000002",
          connection_id: "10000000-0000-4000-8000-000000000002",
          caller: { namespace: "agentplane-visual", name: "retired" },
          status: "revoked",
          revoked_at: "2026-09-09T12:03:00Z",
        })),
      },
    ],
  ],
  ["GET", /^\/connection-service-accounts$/, () => [{ namespace: "agentplane-visual", name: "operator-assistant" }]],
  // The Settings modal mounts all three tabs at once (Mantine keepMounted), so MCP servers and
  // Notifications fetch on mount even while the OAuth clients tab is the one shown in the shot.
  ["GET", /^\/mcp-servers$/, () => MCP_LINKAGES],
  [
    "GET",
    /^\/action-groups$/,
    () =>
      scenario.actionGroupsUnavailable
        ? Response.json({ detail: "the Action Service did not answer: connection refused" }, { status: 502 })
        : MCP_GROUPS,
  ],
  ["GET", /^\/push\/config$/, () => ({ application_server_key: null })],
  ["GET", /^\/push\/subscriptions$/, () => []],
  [
    "POST",
    /^\/connection-enrollments\/[^/]+\/preview$/,
    () => ({
      enrollment: {
        client_id: "test-client-registration-5d46c4f2",
        client_name: "Test external client",
        redirect_uri: "https://test-client.example/oauth/callback",
        expires_at: new Date(NOW + 10 * 60_000).toISOString(),
        version: 1,
      },
      service_accounts: [
        { namespace: "agentplane-visual", name: "public-coder" },
        { namespace: "agentplane-visual", name: "operator-assistant" },
      ],
      connections: [sampleConnection()],
      csrf_token: "test-only-csrf",
      attempted_decision: null,
    }),
  ],
  [
    "POST",
    /^\/actions\/([^/]+)\/decision$/,
    (match) => ({ ...ACTIONS.find((request) => request.id === match[1]), state: "allowed", version: 2 }),
  ],
  ["GET", /^\/sandboxes\/([^/]+)\/egress\/decisions$/, () => egressDecisions()],
  ["GET", /^\/sandboxes\/([^/]+)\/sessions$/, () => SESSIONS.map((session) => toJson(SessionSummarySchema, session))],
  [
    "GET",
    /^\/threads$/,
    (_match, query) =>
      THREADS.filter(
        (thread) =>
          (!query.has("sandbox") || thread.sandbox === query.get("sandbox")) &&
          (!query.has("session_id") || thread.session_id === query.get("session_id"))
      ),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)$/,
    (match) => {
      const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.id === match[1]);
      return thread && scenarioThread(thread);
    },
  ]
);

/** Encode the database-facing Electric row, including PostgreSQL JSONB and bool columns. */
function electricEntity(row: Record<string, unknown>): Record<string, unknown> {
  const value = { ...row };
  for (const field of ["state", "text_ref", "arguments_ref", "output_ref", "input_ref"] as const) {
    if (value[field] !== null) value[field] = JSON.stringify(value[field]);
  }
  value.pending = String(value.pending);
  return value;
}

interface Subset {
  where?: string;
  params?: Record<string, string>;
  order_by?: string;
  limit?: number;
}

function subsetOf(query: URLSearchParams, body: string | undefined): Subset {
  if (query.get("projection_epoch") !== CONVERSATION_EPOCH) {
    throw new Error("thread shapes must name the resolved thread fold projection epoch");
  }
  if (body === undefined) throw new Error("a subset is POSTed with its parameters in the body");
  return JSON.parse(body) as Subset;
}

/** The proxy's entity subset forms, evaluated over the fixture rows. */
function entitySubset(rows: Record<string, unknown>[], subset: Subset): Record<string, unknown>[] {
  const index = (row: Record<string, unknown>) => BigInt(String(row.entity_index));
  const newestFirst = (selected: Record<string, unknown>[]) =>
    selected.sort((left, right) => (index(right) > index(left) ? 1 : index(right) < index(left) ? -1 : 0));
  const selected =
    subset.where === undefined && subset.order_by === "entity_index DESC"
      ? newestFirst(rows)
      : subset.where === "entity_index < $1" && subset.order_by === "entity_index DESC"
        ? newestFirst(rows.filter((row) => index(row) < BigInt(subset.params?.["1"] ?? "0")))
        : subset.where === "entity_kind = 'view_state'"
          ? rows.filter((row) => row.entity_kind === "view_state")
          : subset.where === "entity_kind = 'command' AND pending = true"
            ? rows.filter((row) => row.entity_kind === "command" && row.pending === "true")
            : subset.where === "entity_kind = 'command' AND entity_id = ANY($1)"
              ? rows.filter(
                  (row) =>
                    row.entity_kind === "command" &&
                    (JSON.parse(`[${(subset.params?.["1"] ?? "{}").slice(1, -1)}]`) as string[]).includes(
                      String(row.entity_id)
                    )
                )
              : undefined;
  if (selected === undefined) throw new Error(`the proxy admits no entity subset ${JSON.stringify(subset)}`);
  return selected.slice(0, subset.limit);
}

function shapeRow(relation: string, value: Record<string, unknown>) {
  const identity =
    relation === "thread_entity"
      ? [value.thread_id, value.projection_epoch, value.entity_kind, value.entity_id]
      : [
          value.thread_id,
          value.projection_epoch,
          value.owner_cursor,
          value.owner_id,
          value.field,
          value.generation,
          value.chunk_index,
        ];
  return {
    headers: { relation: ["public", relation] as ["public", string], operation: "insert" as const },
    key: `"public"."${relation}"/${identity.map((part) => JSON.stringify(String(part))).join("/")}`,
    // PostgreSQL JSON columns arrive as their JSON representation; ShapeStream parses this scalar.
    value: relation === "thread_payload_chunk" ? { ...value, text: JSON.stringify(value.text) } : value,
  };
}

/** Positions in the order the fixture lists its rows, which is the order they were first written. */
function threadRows(threadId: string): Record<string, unknown>[] {
  return threadEntityRows(threadId).map((row, index) => electricEntity({ ...row, entity_index: String(index) }));
}

function archivedStderr(cursor: number): Record<string, unknown> {
  return toJson(
    EventEntrySchema,
    create(EventEntrySchema, {
      cursor: BigInt(cursor),
      origin: { sourceId: CONVERSATION_SOURCE, sequence: BigInt(cursor) },
      event: create(EventSchema, {
        observation: { case: "harnessStderr", value: { text: "warning: fixture stderr" } },
      }),
    })
  ) as Record<string, unknown>;
}

function archivedCompletion(cursor: number): Record<string, unknown> {
  return toJson(
    EventEntrySchema,
    create(EventEntrySchema, {
      cursor: BigInt(cursor),
      origin: { sourceId: CONVERSATION_SOURCE, sequence: BigInt(cursor) },
      event: create(EventSchema, {
        observation: { case: "itemCompleted", value: { itemId: "m-2", outcome: { case: "text", value: "complete" } } },
      }),
    })
  ) as Record<string, unknown>;
}

const OBSERVATION_ENTRIES: Record<string, () => Record<string, unknown>> = {
  "31": () => archivedStderr(31),
  "34": () => archivedCompletion(34),
};

function observationPage(threadId: string) {
  return {
    observations: [
      { cursor: "31", kind: "harness_stderr" },
      { cursor: "34", kind: "item_completed" },
    ],
    next_before_cursor: null,
    next_after_cursor: null,
    thread_id: threadId,
  };
}

routes.push(
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/scope$/,
    (match) =>
      scenario.sessionReplay === "unavailable"
        ? // This persistent service failure is distinct from a retired epoch's 410, which the
          // production store resolves by reading the scope again.
          Response.json({ detail: "thread fold is temporarily unavailable" }, { status: 503 })
        : threadScope(match[1]),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/entities$/,
    (match, query, signal) =>
      query.get("live") !== "true"
        ? electricShape([], `visual-entities-${match[1]}`)
        : scenario.sessionReplay === "reconnecting"
          ? Response.json({ detail: "thread shape is temporarily unavailable" }, { status: 503 })
          : electricLive(`visual-entities-${match[1]}`, undefined, signal),
  ],
  [
    "POST",
    /^\/threads\/([0-9a-f-]+)\/sync\/entities$/,
    (match, query, _signal, body) => {
      const rows = threadRows(match[1]).map((row) =>
        scenario.sessionReplay === "catching-up" && row.entity_kind === "view_state"
          ? { ...row, revision_cursor: "8" }
          : row
      );
      return electricSubset(
        entitySubset(rows, subsetOf(query, body)).map((row) => shapeRow("thread_entity", row)),
        `visual-entities-${match[1]}`,
        "thread_entity"
      );
    },
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/chunks\/([a-z_]+)$/,
    (match, query, signal) =>
      query.get("live") === "true"
        ? electricLive(`visual-chunks-${match[1]}-${match[2]}`, "thread_payload_chunk", signal)
        : electricShape([], `visual-chunks-${match[1]}-${match[2]}`, "thread_payload_chunk"),
  ],
  [
    "POST",
    /^\/threads\/([0-9a-f-]+)\/sync\/chunks\/([a-z_]+)$/,
    (match, query, _signal, body) => {
      const params = subsetOf(query, body).params ?? {};
      const rows = Array.from({ length: Object.keys(params).length / 2 }, (_, index) => ({
        ownerId: params[String(2 * index + 1)],
        generation: params[String(2 * index + 2)],
      })).flatMap(({ ownerId, generation }) => {
        const text = payloadBodies.get(payloadKey(ownerId, match[2], generation));
        return text === undefined
          ? []
          : [
              shapeRow("thread_payload_chunk", {
                thread_id: match[1],
                projection_epoch: CONVERSATION_EPOCH,
                owner_cursor: "0",
                owner_id: ownerId,
                field: match[2],
                generation,
                chunk_index: "0",
                text,
              }),
            ];
      });
      return electricSubset(rows, `visual-chunks-${match[1]}-${match[2]}`, "thread_payload_chunk");
    },
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/evidence$/,
    () => ({ observations: [{ observation_cursor: "31", has_native: true }], next_after_cursor: null }),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/evidence\/([0-9]+)\/frames$/,
    (_match) => ({
      frames: [
        {
          source_sequence: "31",
          availability: "present",
          entry: archivedStderr(31),
        },
      ],
      next_after_sequence: null,
    }),
  ],
  // No runner here admits a command, so one the page delivers on load stays unadmitted.
  [
    "POST",
    /^\/threads\/([0-9a-f-]+)\/commands$/,
    () =>
      scenario.commandAdmissionTimedOut
        ? Response.json({ detail: "runner did not admit the command within 15 seconds" }, { status: 504 })
        : UNANSWERED,
  ],
  ["GET", /^\/threads\/([0-9a-f-]+)\/observations$/, (match) => observationPage(match[1])],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/observations\/([0-9]+)$/,
    (match) => ({ cursor: match[2], entry: OBSERVATION_ENTRIES[match[2]]() }),
  ]
);

const FRESH: WatchHealth = {
  fresh: true,
  stale_after_seconds: 900,
  refreshed_seconds_ago: {
    sandboxes: 4.2,
    pods: 3.1,
    egressbindings: 11.7,
    egresspolicies: 11.7,
    actionpolicysets: 8.3,
    actionpolicybindings: 8.3,
  },
};

/** A watch that has stopped cycling: what the `_stale` scenarios have to show rather than hide. */
const WEDGED: WatchHealth = {
  fresh: false,
  stale_after_seconds: 900,
  refreshed_seconds_ago: {
    sandboxes: 2417.4,
    pods: 2417.4,
    egressbindings: 11.7,
    egresspolicies: 11.7,
    actionpolicysets: 8.3,
    actionpolicybindings: 8.3,
  },
};

function watch(): WatchHealth {
  return scenario.wedgedWatch ? WEDGED : FRESH;
}

/** Live inventory and action streams remain EventSource; projected threads use Electric fetches above.
 * A stream a scenario drops goes back to `CONNECTING`, as a browser's does when the network drops,
 * and never reconnects. */
class HarnessEventSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly url: string;
  readyState = HarnessEventSource.CONNECTING;

  constructor(url: string) {
    super();
    this.url = url;
    // After the view's listeners are attached, which happens right after construction.
    setTimeout(() => this.serve(new URL(url, "http://harness")), 0);
  }

  private serve(url: URL): void {
    this.readyState = HarnessEventSource.OPEN;
    if (url.pathname === "/live/threads") {
      const snapshot: ThreadsSnapshot = {
        sandboxes: SANDBOXES,
        threads: THREADS_WITH_SANDBOXES.map(scenarioThread),
        updates_connected: scenario.sidebarSource !== "database-disconnected",
        watch: watch(),
      };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      if (scenario.sidebarSource === "disconnected") this.drop();
      return;
    }
    if (url.pathname === "/live/sandboxes") {
      const snapshot: SandboxesSnapshot = { sandboxes: SANDBOXES, watch: watch() };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      if (scenario.inventoryDropped) this.drop();
      return;
    }
    const sandbox = url.pathname.startsWith("/live/sandboxes/") ? url.pathname.slice("/live/sandboxes/".length) : null;
    if (url.pathname === "/actions/stream") {
      const pending =
        scenario.pendingActions || scenario.route.startsWith("/actions")
          ? ACTIONS.filter((request) => request.state === "decision_pending")
          : [];
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(pending) }));
      return;
    }
    if (sandbox !== null) {
      const snapshot: SandboxSnapshot = {
        sandbox: SANDBOXES.find((row) => row.name === decodeURIComponent(sandbox)) ?? null,
        bindings: BINDINGS,
        action_policy: ACTION_POLICY,
        threads: THREADS,
        watch: watch(),
      };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      return;
    }
    throw new Error(`Unexpected EventSource route: ${url.pathname}`);
  }

  private drop(): void {
    this.readyState = HarnessEventSource.CONNECTING;
    this.dispatchEvent(new Event("error"));
  }

  close(): void {
    this.readyState = HarnessEventSource.CLOSED;
  }
}

window.EventSource = HarnessEventSource as unknown as typeof EventSource;

// Under the frozen clock no stream is ever off for any time at all, so the registry's runs ahead of
// it instead: a stream off since the scene began has been off this long when it renders.
const { outageAge } = scenario;
if (outageAge !== undefined) streamRegistry.now = () => Date.now() + outageAge;

// TODO: Move scene driving and readiness checks to Python Playwright; keep TS scenario fixtures as data.
if (scenario.openConnectionStatus) {
  // Focus opens the indicator's tooltip, as it does for a keyboard or touch reader. Every stream is
  // off until its first frame, so the one to open is the indicator for a stream that has dropped.
  const openStatus = new MutationObserver(() => {
    const indicator = document.querySelector<HTMLElement>('[data-connection][aria-label*="reconnecting"]');
    if (!indicator) return;
    openStatus.disconnect();
    indicator.focus();
  });
  openStatus.observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ["aria-label"] });
}

if (scenario.openDebug) {
  // "Debug history" now lives in the composer's overflow menu: open that first, since Mantine
  // does not mount a closed Menu's dropdown items at all.
  const openMenu = new MutationObserver(() => {
    const trigger = document.querySelector('button[aria-label="More"]');
    if (!(trigger instanceof HTMLButtonElement)) return;
    openMenu.disconnect();
    trigger.click();
    const openDebug = new MutationObserver(() => {
      const item = [...document.querySelectorAll('[role="menuitem"]')].find(
        (candidate) => candidate.textContent === "Debug history"
      );
      if (!(item instanceof HTMLElement)) return;
      openDebug.disconnect();
      item.click();
      if (scenario.openDebug !== "stderr") return;
      const expandStderr = new MutationObserver(() => {
        const control = document.querySelector<HTMLButtonElement>(
          '[data-debug-observation="31"] .agentplane-disclosure-summary'
        );
        if (!control) return;
        expandStderr.disconnect();
        control.click();
      });
      expandStderr.observe(document, { childList: true, subtree: true });
    });
    openDebug.observe(document, { childList: true, subtree: true });
  });
  openMenu.observe(document, { childList: true, subtree: true });
}

if (scenario.openMoreMenu) {
  // Left open, unlike scenario.openDebug's use of the same trigger: this scene's point is the
  // menu's own contents, not a page it navigates to.
  const openMoreMenu = new MutationObserver(() => {
    const trigger = document.querySelector('button[aria-label="More"]');
    if (!(trigger instanceof HTMLButtonElement)) return;
    openMoreMenu.disconnect();
    trigger.click();
  });
  openMoreMenu.observe(document, { childList: true, subtree: true });
}

/** Opens the folded tool-call run, whose steps mount only once it is open. */
function openRun(controls: HTMLButtonElement[]): void {
  controls
    .find(
      (candidate) => candidate.textContent?.includes("tool call") && candidate.getAttribute("aria-expanded") !== "true"
    )
    ?.click();
}

if (scenario.openReasoning) {
  const openReasoning = new MutationObserver(() => {
    const controls = [...document.querySelectorAll<HTMLButtonElement>(".agentplane-disclosure-summary")];
    const step = controls.find(
      (candidate) => candidate.querySelector(".agentplane-step-title")?.textContent === "Reasoning"
    );
    if (!step) {
      openRun(controls);
      return;
    }
    openReasoning.disconnect();
    step.click();
  });
  openReasoning.observe(document, { childList: true, subtree: true });
}

if (scenario.openSetup) {
  const openSetup = new MutationObserver(() => {
    const summary = [...document.querySelectorAll<HTMLElement>(".agentplane-disclosure-summary")].find((candidate) =>
      candidate.textContent?.includes("Thread setup complete")
    );
    if (!summary) return;
    openSetup.disconnect();
    summary.click();
  });
  openSetup.observe(document, { childList: true, subtree: true });
}

/** Opens each folded tool-call line inside the run, which mounts only once the run is open. */
function openToolLines(): void {
  for (const step of document.querySelectorAll<HTMLButtonElement>(
    ".agentplane-step-details .agentplane-disclosure-summary"
  )) {
    if (
      step.getAttribute("aria-expanded") !== "true" &&
      step.querySelector(".agentplane-step-title")?.textContent !== "Reasoning"
    )
      step.click();
  }
}

if (scenario.openRun) {
  const openFoldedRun = new MutationObserver(() => {
    const controls = [...document.querySelectorAll<HTMLButtonElement>(".agentplane-disclosure-summary")];
    openRun(controls);
    if (document.querySelector(".agentplane-step-details")) openFoldedRun.disconnect();
  });
  openFoldedRun.observe(document, { childList: true, subtree: true });
}

if (scenario.openToolPayloads) {
  const openToolPayloads = new MutationObserver(() => {
    openRun([...document.querySelectorAll<HTMLButtonElement>(".agentplane-disclosure-summary")]);
    openToolLines();
  });
  openToolPayloads.observe(document, { childList: true, subtree: true });
}

if (scenario.openRecoveryDetails) {
  const openRecovery = new MutationObserver(() => {
    const controls = [...document.querySelectorAll<HTMLButtonElement>(".agentplane-disclosure-summary")];
    openRun(controls);
    for (const summary of controls) {
      if (
        summary.getAttribute("aria-expanded") !== "true" &&
        summary.textContent?.includes("not retained in model context")
      ) {
        summary.click();
      }
    }
    openToolLines();
  });
  openRecovery.observe(document, { childList: true, subtree: true });
}

if (scenario.openEvidence) {
  const openEvidence = new MutationObserver(() => {
    const button = document.querySelector<HTMLButtonElement>(
      `[data-thread-anchor="${scenario.openEvidence}"] button[aria-label="Evidence"]`
    );
    if (!button) return;
    openEvidence.disconnect();
    button.click();
  });
  openEvidence.observe(document, { childList: true, subtree: true });
}

if (scenario.openClampedBlocks) {
  const openClamped = () => {
    const unopened = [
      ...document.querySelectorAll<HTMLButtonElement>(".agentplane-clamped-block button[aria-expanded='false']"),
    ];
    for (const button of unopened) button.click();
    const toolLines = [
      ...document.querySelectorAll<HTMLButtonElement>(".agentplane-step-details .agentplane-disclosure-summary"),
    ];
    const loading = document.querySelector(".agentplane-step-details [aria-busy='true']");
    if (
      unopened.length === 0 &&
      toolLines.length > 0 &&
      toolLines.every((button) => button.getAttribute("aria-expanded") === "true") &&
      !loading
    ) {
      openClampedBlocks.disconnect();
    }
  };
  // Tool arguments and output bodies load independently. Keep watching after the first block opens
  // so a later payload is expanded too.
  const openClampedBlocks = new MutationObserver(openClamped);
  openClampedBlocks.observe(document, { childList: true, subtree: true });
  openClamped();
}

if (scenario.preselectReconnect) {
  const selectExisting = new MutationObserver(() => {
    const connection = document.querySelector<HTMLSelectElement>('select[name="connection"]');
    const account = document.querySelector<HTMLSelectElement>('select[name="service_account"]');
    if (!connection || !account) return;
    selectExisting.disconnect();
    connection.value = sampleConnection().id;
    connection.dispatchEvent(new Event("change", { bubbles: true }));
    account.value = "agentplane-visual/operator-assistant";
    account.dispatchEvent(new Event("change", { bubbles: true }));
  });
  selectExisting.observe(document, { childList: true, subtree: true });
}
if (scenario.checkComposerControls) {
  const checkControls = new MutationObserver(() => {
    const send = document.querySelector<HTMLElement>('.agentplane-composer-send button[aria-label="Send"]');
    const effort = document.querySelector<HTMLElement>(".agentplane-composer-effort");
    const model = document.querySelector<HTMLElement>(".agentplane-composer-model");
    const controls = document.querySelector<HTMLElement>(".agentplane-composer-controls");
    const indicator = document.querySelector(".agentplane-topbar-title .agentplane-thread-status-indicator");
    if (!send || !effort || !model || !controls || !indicator) return;
    requestAnimationFrame(() => {
      const box = send.getBoundingClientRect();
      const modelBox = model.getBoundingClientRect();
      const effortBox = effort.getBoundingClientRect();
      if (
        box.width > 0 &&
        box.left >= 0 &&
        box.right <= window.innerWidth &&
        modelBox.width > 0 &&
        effortBox.width > 0 &&
        Math.abs(modelBox.top - box.top) < 8 &&
        Math.abs(effortBox.top - box.top) < 8
      ) {
        controls.dataset.composerLayoutReady = "true";
        checkControls.disconnect();
      }
    });
  });
  checkControls.observe(document, { childList: true, subtree: true });
}
if (scenario.scrollActionReview) {
  const scrollReview = new MutationObserver(() => {
    const details = document.querySelector<HTMLElement>(".action-affordance-details:not([hidden])");
    if (!details?.querySelector("button")) return;
    scrollReview.disconnect();
    requestAnimationFrame(() => {
      details.scrollTop = details.scrollHeight;
      if (details.scrollTop > 0) details.dataset.scrollReady = "true";
    });
  });
  scrollReview.observe(document, { childList: true, subtree: true, attributes: true });
}
if (scenario.openActionReview) {
  const openActionReview = new MutationObserver(() => {
    const button = document.querySelector<HTMLButtonElement>('button[aria-label="Review pending actions"]');
    if (!button) return;
    openActionReview.disconnect();
    button.click();
  });
  openActionReview.observe(document, { childList: true, subtree: true });
}
if (scenario.openSettings) {
  // There's no dedicated route for the Settings modal; open it the way an operator would, by
  // clicking the sidebar's gear button, rather than a URL that only exists for this test.
  const openSettings = new MutationObserver(() => {
    const button = document.querySelector('button[aria-label="Settings"]');
    if (!button) return;
    openSettings.disconnect();
    (button as HTMLButtonElement).click();
  });
  openSettings.observe(document, { childList: true, subtree: true });
}
if (scenario.showArchived) {
  // The switch is sidebar state with no URL param; flip it the way an operator would.
  const showArchived = new MutationObserver(() => {
    const toggle = document.querySelector<HTMLInputElement>('input[aria-label="Show archived threads"]');
    if (!toggle) return;
    showArchived.disconnect();
    toggle.click();
  });
  showArchived.observe(document, { childList: true, subtree: true });
}
if (scenario.openRaw) {
  // No URL param toggles a Raw switch; flip each one as it mounts, the way an operator would.
  const flipped = new WeakSet<HTMLLabelElement>();
  new MutationObserver(() => {
    for (const label of document.querySelectorAll("label")) {
      if (label.textContent !== "Raw" || flipped.has(label)) continue;
      flipped.add(label);
      label.click();
    }
  }).observe(document, { childList: true, subtree: true });
}
if (scenario.openMobileSidebar) {
  // The drawer has no route of its own; open it the way an operator would, by tapping the
  // phone-width hamburger.
  const openMobileSidebar = new MutationObserver(() => {
    const button = document.querySelector('button[aria-label="Toggle navigation"]');
    if (!button) return;
    openMobileSidebar.disconnect();
    (button as HTMLButtonElement).click();
  });
  openMobileSidebar.observe(document, { childList: true, subtree: true });
}
if (!scenario.disclosureVisual) window.location.hash = scenario.route;

const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(
  <ThemeProvider>
    {scenario.disclosureVisual ? <DisclosureVisual stage={scenario.disclosureVisual} /> : <App />}
  </ThemeProvider>
);

if (scenario.claudePaused) {
  const openHarness = new MutationObserver(() => {
    const label = [...document.querySelectorAll("label")].find((node) => node.textContent === "Harness");
    const control = label?.control;
    if (!(control instanceof HTMLInputElement) || control.value !== "Codex") return;
    openHarness.disconnect();
    control.click();
  });
  openHarness.observe(document, { childList: true, subtree: true, attributes: true });
}
