/**
 * Visual-test harness: the app mounted on canned data, nothing on the network. The `?page=` query
 * (set by visual-test-lib) picks the route; `fetch` (stubbed by network.ts, imported first so the
 * app's client captures the stub) answers API and Electric Shape routes. `EventSource` remains
 * only for the live sandbox, thread, and action-inventory views.
 */
import "./network";
import "@mantine/core/styles.css";

import { create, toJson } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import App from "../app";
import { sampleConnection } from "../connections_fixture";
import type {
  ActionPolicyView,
  ActionRequestView,
  BindingView,
  Decision,
  PolicyView,
  SandboxView,
  ThreadView,
} from "../client";
import type { SandboxesSnapshot, SandboxSnapshot, ThreadsSnapshot, WatchHealth } from "../live";
import { EventSchema, ItemKind, TurnStatus } from "../../../protocol/event_pb";
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
import { electricShape, electricSubset, routes } from "./network";
import { SCENARIOS, type Scenario } from "./scenarios";
import { LocalCommands } from "../local_commands";

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

// visual-test-lib freezes the wall clock before this bundle runs, so relative ages stay put.
const NOW = Date.now();
const HOUR = 3_600_000;

function ago(ms: number): string {
  return new Date(NOW - ms).toISOString();
}

const SANDBOXES: SandboxView[] = [
  {
    name: "demo-a1b2",
    uid: "0f9c1d2e-0000-4000-8000-00000000a1b2",
    state: "running",
    created_at: ago(3 * HOUR),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "demo-a1b2" },
    conditions: [{ type: "Ready", status: "True", reason: "PodReady", message: null }],
    node_name: "harness-node",
    pod: {
      phase: "Running",
      ip: "10.0.0.7",
      node_name: "harness-node",
      reason: null,
      message: null,
      conditions: [
        { type: "PodScheduled", status: "True", reason: null, message: null },
        { type: "Ready", status: "True", reason: null, message: null },
      ],
      containers: [{ name: "runner", state: "running", reason: null, message: null, ready: true, restart_count: 0 }],
    },
  },
  {
    name: "codex-c3d4",
    uid: "0f9c1d2e-0000-4000-8000-00000000c3d4",
    state: "waiting_for_pod_ready",
    created_at: ago(2 * 60_000),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "codex-c3d4" },
    conditions: [{ type: "Ready", status: "False", reason: "PodPending", message: null }],
    node_name: "harness-node",
    pod: {
      phase: "Pending",
      ip: null,
      node_name: "harness-node",
      reason: null,
      message: null,
      conditions: [{ type: "Ready", status: "False", reason: "ContainersNotReady", message: null }],
      containers: [
        {
          name: "runner",
          state: "waiting",
          reason: "ImagePullBackOff",
          message: 'Back-off pulling image "registry.test/agentplane-runner:harness"',
          ready: false,
          restart_count: 0,
        },
      ],
    },
  },
  {
    name: "old-e5f6",
    uid: "0f9c1d2e-0000-4000-8000-00000000e5f6",
    state: "suspended",
    created_at: ago(48 * HOUR),
    operating_mode: "Suspended",
    service_account: { namespace: "agentplane-visual", name: "old-e5f6" },
    conditions: [{ type: "Ready", status: "False", reason: "Suspended", message: null }],
    node_name: null,
    pod: null,
  },
];

if (scenario.threadlessSandbox) {
  SANDBOXES.push({
    name: "test-provisioning",
    uid: "0f9c1d2e-0000-4000-8000-000000000007",
    state: "waiting_for_pod",
    created_at: ago(30_000),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "test-provisioning" },
    conditions: [],
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
    name: "demo-a1b2-7q4xk",
    from_git: false,
    subjects: [{ namespace: "agentplane-visual", name: "demo-a1b2" }],
    expires_at: ago(2 * HOUR),
    policies: [POLICIES[1]],
    missing_policies: [],
  },
  {
    name: "demo-a1b2-github-public",
    from_git: true,
    subjects: [{ namespace: "agentplane-visual", name: "demo-a1b2" }],
    expires_at: null,
    policies: [POLICIES[0]],
    missing_policies: [],
  },
];

/**
 * What the Action Service auto-decides for demo-a1b2: the binding the app wrote at launch, one the
 * operator added for the afternoon, and every state a set can be in -- parsed and judged, edited
 * since it was judged, refused, and missing.
 */
const ACTION_POLICY: ActionPolicyView = {
  synced: true,
  bindings: [
    {
      name: "demo-a1b2-k2m9x",
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
      name: "demo-a1b2-push-afternoon",
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
      binding: "demo-a1b2-k2m9x",
      policy_set: "public-coder",
      index: 0,
      policy: { type: "exact_actions", actions: { github: ["get_file_contents", "list_commits", "search_code"] } },
    },
    {
      binding: "demo-a1b2-k2m9x",
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
      binding: "demo-a1b2-k2m9x",
      policy_set: "harness-reviews",
      index: 0,
      policy: { type: "exact_actions", actions: { github: ["pull_request_read", "list_pull_requests"] } },
    },
    {
      binding: "demo-a1b2-push-afternoon",
      policy_set: "harness-push",
      index: 0,
      policy: {
        type: "argument_schema",
        actions: { github: ["push_files"] },
        argument_schema: { properties: { branch: { pattern: "^harness/" } }, required: ["branch"] },
      },
    },
  ],
  auto_deny_if: [
    {
      binding: "demo-a1b2-push-afternoon",
      policy_set: "harness-push",
      index: 0,
      policy: { type: "exact_actions", actions: { kubernetes: ["pods_delete", "resources_delete"] } },
    },
  ],
  auto_deny_unless: [],
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
    binding: "demo-a1b2-github-public",
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
    binding: "demo-a1b2-github-public",
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
  create(SessionSummarySchema, { sessionId: "s-0", spec: SPEC, lastCursor: 31n, harnessState: HarnessState.STOPPED }),
];

/** The store's copy of the sessions: s-1 named, s-0 not, so both renderings are on the page. */
const THREADS: ThreadView[] = [
  {
    id: "5f1c4a2e-0000-4000-8000-000000000001",
    sandbox: "demo-a1b2",
    session_id: "s-1",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(HOUR),
    name: "List the repository files",
    archived: false,
    last_cursor: 14,
    last_event_at: ago(60_000),
    harness_state: "HARNESS_STATE_RUNNING",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000000",
    sandbox: "demo-a1b2",
    session_id: "s-0",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(2 * HOUR),
    name: null,
    archived: false,
    last_cursor: 31,
    last_event_at: ago(90 * 60_000),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000002",
    sandbox: "demo-a1b2",
    session_id: "s-2",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(30 * 60_000),
    name: "Clean up the stale branch",
    archived: false,
    last_cursor: 23,
    last_event_at: ago(10_000),
    harness_state: "HARNESS_STATE_RUNNING",
  },
];

/**
 * The sidebar's cross-sandbox fixture: the three THREADS above
 * (all `demo-a1b2`), one each for the pending and suspended sandboxes, one archived, and one whose
 * `sandbox` names no live SandboxView at all -- the struck-through, read-only group.
 */
const THREADS_WITH_SANDBOXES: ThreadView[] = [
  ...THREADS,
  {
    id: "5f1c4a2e-0000-4000-8000-000000000003",
    sandbox: "codex-c3d4",
    session_id: "s-3",
    harness: "HARNESS_CODEX",
    model: "harness-codex-model",
    cwd: "/state/work",
    created_at: ago(5 * 60_000),
    name: "Watch the image build",
    archived: false,
    last_cursor: 2,
    last_event_at: ago(5 * 60_000),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000004",
    sandbox: "old-e5f6",
    session_id: "s-4",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(47 * HOUR),
    name: "Investigate flaky CI",
    archived: false,
    last_cursor: 9,
    last_event_at: ago(46 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000005",
    sandbox: "old-debug-3f9c",
    session_id: "s-5",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(72 * HOUR),
    name: "Why did the migration hang",
    archived: false,
    last_cursor: 18,
    last_event_at: ago(70 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000006",
    sandbox: "demo-a1b2",
    session_id: "s-6",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(96 * HOUR),
    name: "Old flaky-test spike",
    archived: true,
    last_cursor: 3,
    last_event_at: ago(95 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
  },
];

const ACTIONS: ActionRequestView[] = [
  {
    id: "70000000-0000-4000-8000-000000000001",
    action: { group: "everything", name: "echo" },
    arguments: { repository: "test-owner/test-repository", token: "[redacted]" },
    title: "echo the test repository handle back",
    description: "Confirms the fixture connection still reaches the echo Action before the demo run.",
    origin: { thread_id: THREADS[0].id },
    correlation: {},
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
    id: "70000000-0000-4000-8000-000000000002",
    action: { group: "everything", name: "echo" },
    arguments: { message: "completed fixture execution" },
    title: "echo the completed fixture message",
    description: null,
    origin: { thread_id: THREADS[1].id },
    correlation: {},
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
      result: { echo: { message: "completed fixture execution" } },
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
    origin: { thread_id: THREADS[1].id },
    correlation: {},
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
    origin: { thread_id: THREADS[2].id },
    correlation: {},
    idempotency_key: "visual-auto-approved",
    caller: { namespace: "agentplane-visual", name: "demo-a1b2" },
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
        bindings: [{ namespace: "agentplane-visual", name: "demo-a1b2-github-public", resource_version: "12345" }],
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
];

const CONVERSATION_SOURCE = "visual-runner";
const CONVERSATION_EPOCH = "20260921";
const payloadBodies = new Map<string, string>();

function payloadKey(
  ownerCursor: string,
  ownerId: string,
  field: string,
  generation: string,
  revisionCursor: string
): string {
  return `${ownerCursor}:${ownerId}:${field}:${generation}:${revisionCursor}`;
}

function payload(
  ownerCursor: number,
  ownerId: string,
  field: string,
  body: string,
  revisionCursor = ownerCursor
): Record<string, string> {
  const reference = {
    source_id: CONVERSATION_SOURCE,
    projection_epoch: CONVERSATION_EPOCH,
    owner_cursor: String(ownerCursor),
    owner_item_id: ownerId,
    field,
    generation: "1",
    revision_cursor: String(revisionCursor),
  };
  payloadBodies.set(payloadKey(reference.owner_cursor, ownerId, field, "1", reference.revision_cursor), body);
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
    source_id: CONVERSATION_SOURCE,
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
        active_turn_id: activeTurn,
        harness_state: activeTurn === null ? "stopped" : "running",
      },
      unresolved_count: 0,
      operational: {
        operational_version: String(throughCursor),
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
      completion: extra.complete === false ? null : text,
      tool_succeeded: extra.output === undefined ? null : true,
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
  reason: string | null = null
): Record<string, unknown> {
  return entity(
    "command",
    id,
    cursor,
    { operation, outcome, outcome_cursor: outcome === "pending" ? null : String(cursor), outcome_reason: reason },
    { pending: outcome === "pending" }
  );
}

function standardRows(threadId: string): Record<string, unknown>[] {
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
    item(20, "r-1", ItemKind.REASONING, "I will inspect the repository structure before proposing a change.", {
      threadId,
    }),
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

function statesRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(23, "t2"),
    item(7, "tool-0", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: '{"command":"git branch -d stale"}',
      output: "fatal: branch 'stale' not found.",
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
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function conversationRows(threadId: string): Record<string, unknown>[] {
  if (scenario.failedTurn) return failedRows(threadId, scenario.failedTurn === "after-content");
  if (scenario.interleavedEvents) return interleavedRows(threadId);
  if (threadId === THREADS[2].id || scenario.pendingCommands) return statesRows(threadId);
  return standardRows(threadId);
}

function conversationInterest(threadId: string): Record<string, string | null> {
  const through = conversationRows(threadId).find((row) => row.entity_kind === "view_state")?.revision_cursor ?? "0";
  return {
    source_id: CONVERSATION_SOURCE,
    projection_epoch: CONVERSATION_EPOCH,
    through_cursor: String(through),
    anchor_cursor: String(through),
    tail_from: "0",
    window_from: null,
    window_before: null,
  };
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

// Only what a page still asks for: the sandboxes, their bindings and their threads arrive on the
// live streams above.
routes.push(
  [
    "GET",
    /^\/models$/,
    () => ({ HARNESS_CLAUDE: ["harness-claude-model", "next-model"], HARNESS_CODEX: ["harness-codex-model"] }),
  ],
  [
    "GET",
    /^\/presets$/,
    () => [
      {
        name: "public-coder",
        title: "Public coder",
        template: "agentplane-runner",
        policies: ["github-public"],
        action_policy_sets: ["public-coder"],
        thread_defaults: {
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
  ["GET", /^\/egress\/policies$/, () => POLICIES],
  ["GET", /^\/action-policy\/sets$/, () => ACTION_POLICY.bindings.flatMap((binding) => binding.policy_sets)],
  ["GET", /^\/actions$/, () => ACTIONS],
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
  // The Settings modal mounts all three tabs at once (Mantine keepMounted); MCP servers and
  // Notifications fetch on mount even while the OAuth clients tab is the one shown in the shot.
  ["GET", /^\/mcp-servers$/, () => []],
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
  ["GET", /^\/threads\/([0-9a-f-]+)$/, (match) => THREADS_WITH_SANDBOXES.find((thread) => thread.id === match[1])]
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

/** Mutable Electric collections bootstrap their fixed server-selected interest through an on-demand subset. */
function currentSubset(query: URLSearchParams): boolean {
  if (!query.has("subset__where") && !query.has("subset__params")) return false;
  if (query.get("subset__where") !== "true = true" || query.get("subset__params") !== "{}") {
    throw new Error("current Electric shapes must request the fixed true = true subset with empty parameters");
  }
  if (query.get("offset") !== "now") throw new Error("current Electric snapshots must start the stream at offset now");
  if (query.get("source_id") !== CONVERSATION_SOURCE || query.get("projection_epoch") !== CONVERSATION_EPOCH) {
    throw new Error("current Electric shapes must select the resolved conversation source and projection epoch");
  }
  return true;
}

function shapeRow(relation: string, value: Record<string, unknown>) {
  const identity =
    relation === "conversation_entity"
      ? [value.thread_id, value.source_id, value.projection_epoch, value.entity_kind, value.entity_id]
      : [
          value.thread_id,
          value.source_id,
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
    value,
  };
}

function threadRows(threadId: string): Record<string, unknown>[] {
  return conversationRows(threadId).map(electricEntity);
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

function observationPage(threadId: string) {
  return {
    observations: [
      {
        cursor: "31",
        source_id: CONVERSATION_SOURCE,
        source_sequence: "31",
        kind: "harness_stderr",
        entry: archivedStderr(31),
      },
      {
        cursor: "34",
        source_id: CONVERSATION_SOURCE,
        source_sequence: "34",
        kind: "item_completed",
        entry: archivedCompletion(34),
      },
    ],
    next_before_cursor: null,
    next_after_cursor: null,
    thread_id: threadId,
  };
}

routes.push(
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/interest$/,
    (match) =>
      scenario.sessionReplay === "gap"
        ? Response.json({ detail: "conversation scope expired; refetch the current projection" }, { status: 410 })
        : conversationInterest(match[1]),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/entities$/,
    (match, query) => {
      const subset = currentSubset(query);
      if (!subset && query.get("offset") !== null) {
        return electricShape([], `visual-entities-${match[1]}`);
      }
      if (!subset) throw new Error("current Electric shapes must begin with a subset snapshot");
      const rows = threadRows(match[1]).map((row) => {
        if (scenario.sessionReplay !== "catching-up" || row.entity_kind !== "view_state") return row;
        return { ...row, revision_cursor: "8" };
      });
      return electricSubset(
        rows.map((row) => shapeRow("conversation_entity", row)),
        `visual-entities-${match[1]}`
      );
    },
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/commands$/,
    (match, query) => {
      const subset = currentSubset(query);
      if (!subset && query.get("offset") !== null) {
        return electricShape([], `visual-commands-${match[1]}`);
      }
      if (!subset) throw new Error("current Electric command shapes must begin with a subset snapshot");
      const selected = new Set(query.getAll("command_id"));
      const rows = threadRows(match[1]).filter(
        (row) => row.entity_kind === "command" && selected.has(String(row.entity_id))
      );
      return electricSubset(
        rows.map((row) => shapeRow("conversation_entity", row)),
        `visual-commands-${match[1]}`
      );
    },
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/payload-interest$/,
    (_match, query) => {
      const ownerCursor = query.get("owner_cursor") ?? "0";
      const ownerId = query.get("owner_id") ?? "";
      const field = query.get("field") ?? "";
      const generation = query.get("generation") ?? "0";
      const revisionCursor = query.get("revision_cursor") ?? "0";
      const body = payloadBodies.get(payloadKey(ownerCursor, ownerId, field, generation, revisionCursor));
      return {
        source_id: CONVERSATION_SOURCE,
        projection_epoch: CONVERSATION_EPOCH,
        owner_cursor: ownerCursor,
        owner_id: ownerId,
        field,
        generation,
        revision_cursor: revisionCursor,
        present: body !== undefined,
        chunk_count: body === undefined ? "0" : "1",
        content_bytes: String(new TextEncoder().encode(body ?? "").byteLength),
      };
    },
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/payload-chunks$/,
    (match, query) => {
      const ownerCursor = query.get("owner_cursor") ?? "0";
      const ownerId = query.get("owner_id") ?? "";
      const field = query.get("field") ?? "";
      const generation = query.get("generation") ?? "0";
      const revisionCursor = query.get("revision_cursor") ?? "0";
      const body = payloadBodies.get(payloadKey(ownerCursor, ownerId, field, generation, revisionCursor));
      const rows =
        query.get("offset") !== null && query.get("offset") !== "-1"
          ? []
          : body === undefined
            ? []
            : [
                shapeRow("conversation_payload_chunk", {
                  thread_id: match[1],
                  source_id: CONVERSATION_SOURCE,
                  projection_epoch: CONVERSATION_EPOCH,
                  owner_cursor: ownerCursor,
                  owner_id: ownerId,
                  field,
                  generation,
                  chunk_index: "0",
                  text: body,
                }),
              ];
      return electricShape(rows, `visual-payload-${ownerCursor}-${ownerId}-${field}`);
    },
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/conversation\/evidence$/,
    () => ({ observations: [{ observation_cursor: "31", has_native: true }], next_after_cursor: null }),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/conversation\/evidence\/([0-9]+)\/frames$/,
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
  ["GET", /^\/threads\/([0-9a-f-]+)\/conversation\/observations$/, (match) => observationPage(match[1])]
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

/** Live inventory and action streams remain EventSource; projected conversations use Electric fetches above. */
class HarnessEventSource extends EventTarget {
  readonly url: string;
  readyState = 1;

  constructor(url: string) {
    super();
    this.url = url;
    // After the view's listeners are attached, which happens right after construction.
    setTimeout(() => this.serve(new URL(url, "http://harness")), 0);
  }

  private serve(url: URL): void {
    if (url.pathname === "/live/threads") {
      const snapshot: ThreadsSnapshot = {
        sandboxes: SANDBOXES,
        threads: THREADS_WITH_SANDBOXES,
        updates_connected: scenario.sidebarSource !== "database-disconnected",
        watch: watch(),
      };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      if (scenario.sidebarSource === "disconnected") this.dispatchEvent(new Event("error"));
      return;
    }
    if (url.pathname === "/live/sandboxes") {
      const snapshot: SandboxesSnapshot = { sandboxes: SANDBOXES, watch: watch() };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      return;
    }
    const sandbox = url.pathname.startsWith("/live/sandboxes/") ? url.pathname.slice("/live/sandboxes/".length) : null;
    if (url.pathname === "/actions/stream") {
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(ACTIONS) }));
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

  close(): void {
    this.readyState = 2;
  }
}

window.EventSource = HarnessEventSource as unknown as typeof EventSource;

if (scenario.openDebug) {
  const openDebug = new MutationObserver(() => {
    const button = [...document.querySelectorAll("button")].find(
      (candidate) => candidate.textContent === "Debug history"
    );
    if (!(button instanceof HTMLButtonElement)) return;
    openDebug.disconnect();
    button.click();
    if (scenario.openDebug !== "stderr") return;
    const expandStderr = new MutationObserver(() => {
      const row = document.querySelector<HTMLDetailsElement>('[data-debug-observation="31"]');
      if (!row) return;
      expandStderr.disconnect();
      row.open = true;
      row.dispatchEvent(new Event("toggle", { bubbles: true }));
    });
    expandStderr.observe(document, { childList: true, subtree: true });
  });
  openDebug.observe(document, { childList: true, subtree: true });
}

if (scenario.openReasoning) {
  const openReasoning = new MutationObserver(() => {
    const summary = [...document.querySelectorAll("summary")].find(
      (candidate) => candidate.textContent === "Reasoning"
    );
    if (!(summary instanceof HTMLElement)) return;
    openReasoning.disconnect();
    summary.click();
  });
  openReasoning.observe(document, { childList: true, subtree: true });
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
if (scenario.openActionPolicySets) {
  // Once the preset's pick has landed as a pill, open the sets dropdown so the shot carries the
  // namespace's options beside the pre-filled pick.
  const openSets = new MutationObserver(() => {
    const pill = [...document.querySelectorAll(".mantine-Pill-root")].find(
      (node) => node.textContent?.trim() === "public-coder"
    );
    const label = [...document.querySelectorAll("label")].find((node) => node.textContent === "Action policy sets");
    const control = label?.control;
    if (!pill || !(control instanceof HTMLInputElement)) return;
    openSets.disconnect();
    control.click();
  });
  openSets.observe(document, { childList: true, subtree: true });
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
if (scenario.openRawStatus) {
  // No URL param toggles the switch (unlike the tab itself); flip it the way an operator would.
  const openRaw = new MutationObserver(() => {
    const label = [...document.querySelectorAll("label")].find((candidate) => candidate.textContent === "Raw");
    if (!label) return;
    openRaw.disconnect();
    label.click();
  });
  openRaw.observe(document, { childList: true, subtree: true });
}
if (scenario.openMobileSidebar) {
  // The drawer has no route of its own; open it the way an operator would, by tapping the
  // phone-width hamburger.
  const openMobileSidebar = new MutationObserver(() => {
    const button = document.querySelector('button[aria-label="Open navigation"]');
    if (!button) return;
    openMobileSidebar.disconnect();
    (button as HTMLButtonElement).click();
  });
  openMobileSidebar.observe(document, { childList: true, subtree: true });
}
window.location.hash = scenario.route;

const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(
  <MantineProvider defaultColorScheme="auto">
    <App />
  </MantineProvider>
);
