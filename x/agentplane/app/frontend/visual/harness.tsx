/**
 * Visual-test harness: the app mounted on canned data, nothing on the network. The `?page=` query
 * (set by visual-test-lib) picks the route; `fetch` (stubbed by network.ts, imported first so the
 * app's client captures the stub) answers the routes a page still asks for, and `EventSource`
 * serves both stream shapes: one snapshot per live view, and one turn of runner events into the
 * session view.
 */
import "./network";
import "@mantine/core/styles.css";

import { create, toJson, toJsonString, type MessageInitShape } from "@bufbuild/protobuf";
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
import type { SandboxesSnapshot, SandboxSnapshot, WatchHealth } from "../live";
import {
  AttachedSchema,
  Direction,
  EventSchema,
  HarnessState,
  ItemKind,
  Provider,
  SessionSpecSchema,
  SessionSummarySchema,
  TurnStatus,
  type Attached,
  type Event,
  type SessionSpec,
  type SessionSummary,
} from "../protocol_pb";
import { routes } from "./network";
import { SCENARIOS, type Scenario } from "./scenarios";

/** Resolved before any fixture is built: the scenario's fields are what the fixtures vary on. */
function resolveScenario(): Scenario {
  const name = new URLSearchParams(window.location.search).get("page") ?? "sandboxes";
  const found: Scenario | undefined = SCENARIOS[name];
  if (found === undefined) throw new Error(`unknown harness scenario ${name}`);
  return found;
}

const scenario = resolveScenario();

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
    conditions: [{ type: "Ready", status: "False", reason: "Suspended", message: null }],
    node_name: null,
    pod: null,
  },
];

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
    subjects: ["demo-a1b2"],
    expires_at: ago(2 * HOUR),
    policies: [POLICIES[1]],
    missing_policies: [],
  },
  {
    name: "demo-a1b2-github-public",
    from_git: true,
    subjects: ["demo-a1b2"],
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
      policy_sets: [
        {
          name: "public-coder",
          generation: 2,
          ready: { status: "True", reason: "Valid", message: "spec accepted", observed_generation: 2 },
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
  provider: Provider.CLAUDE,
  cwd: "/state/work",
  reasoningEffort: "low",
});

const SESSIONS: SessionSummary[] = [
  create(SessionSummarySchema, { sessionId: "s-1", spec: SPEC, lastSequence: 14n, harness: HarnessState.RUNNING }),
  create(SessionSummarySchema, { sessionId: "s-0", spec: SPEC, lastSequence: 31n, harness: HarnessState.STOPPED }),
];

/** The store's copy of the sessions: s-1 named, s-0 not, so both renderings are on the page. */
const THREADS: ThreadView[] = [
  {
    id: "5f1c4a2e-0000-4000-8000-000000000001",
    sandbox: "demo-a1b2",
    session_id: "s-1",
    provider: "PROVIDER_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(HOUR),
    name: "List the repository files",
    archived: false,
    last_sequence: 14,
    last_event_at: ago(60_000),
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000000",
    sandbox: "demo-a1b2",
    session_id: "s-0",
    provider: "PROVIDER_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(2 * HOUR),
    name: null,
    archived: false,
    last_sequence: 31,
    last_event_at: ago(90 * 60_000),
  },
  {
    id: "5f1c4a2e-0000-4000-8000-000000000002",
    sandbox: "demo-a1b2",
    session_id: "s-2",
    provider: "PROVIDER_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(30 * 60_000),
    name: "Clean up the stale branch",
    archived: false,
    last_sequence: 23,
    last_event_at: ago(10_000),
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
    caller_principal: "service-account:agentplane-visual:test-public-coder",
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
    caller_principal: "system:serviceaccount:test-agentplane:test-workload",
    state: "succeeded",
    version: 4,
    created_at: ago(40 * 60_000),
    updated_at: ago(39 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000002",
      verdict: "allow",
      provider: "human_operator",
      issuer: "test-operator",
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
    caller_principal: "service-account:agentplane-visual:test-public-coder",
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
      issuer: "test-operator",
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
    caller_principal: "kubernetes-sandbox:demo-a1b2",
    state: "succeeded",
    version: 3,
    created_at: ago(15 * 60_000),
    updated_at: ago(14 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000004",
      verdict: "allow",
      provider: "policy_engine",
      issuer: "policy_engine",
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
      // The MCP tool-result content-block shape: a `content` array whose entries are themselves
      // JSON-encoded strings, the way a real GitHub search_code call returns them. Exercises the
      // Result view's nested-JSON parsing rather than a plain value like the other fixtures'.
      result: {
        content: [
          JSON.stringify({
            total_count: 1,
            items: [{ path: "x/agentplane/action_service/policies/policy.py", repository: "agentydragon/ducktape" }],
          }),
        ],
      },
      error: null,
      created_at: ago(14 * 60_000),
      started_at: ago(14 * 60_000 - 500),
      completed_at: ago(14 * 60_000 - 900),
      reconciled_at: null,
    },
  },
];

const ATTACHED: Attached = create(AttachedSchema, {
  sessionId: "s-1",
  spec: SPEC,
  lastSequence: 14n,
  harness: HarnessState.RUNNING,
});

const ATTACHED_STATES: Attached = create(AttachedSchema, {
  sessionId: "s-2",
  spec: SPEC,
  lastSequence: 23n,
  harness: HarnessState.RUNNING,
});

/** Real reasoning is several sentences, so the folded block is worth opening. */
const THINKING = [
  "The user asked what is in the repository, not for a recursive listing.",
  "A plain ls of the top level answers it; anything deeper buries the answer.",
].join("\n");

/**
 * The second turn's thinking. The view scrolls to the newest event, so the reasoning that the
 * expanded scenario has to show is the one in the last turn.
 */
const THINKING_AGAIN = [
  "src is a directory, so reading it starts with listing what is inside.",
  "Only then is there a file to open, and the user did not name one.",
].join("\n");

/** The markdown an answer actually arrives as: headings, a list, inline code, a fence, emphasis. */
const ANSWER = [
  "## Repository root",
  "",
  "Two entries, both tracked:",
  "",
  "- `README.md` — the project overview",
  "- `src/` — **all** the source, including the _experimental_ parts",
  "",
  "Run the tests with:",
  "",
  "```bash",
  "bazel test //...",
  "```",
].join("\n");

function event(
  sequence: number,
  observation: MessageInitShape<typeof EventSchema>["observation"],
  sources: number[] = []
): Event {
  return create(EventSchema, { sequence: BigInt(sequence), observation, sourceSequences: sources.map(BigInt) });
}

function frame(
  direction: Direction,
  payload: Record<string, unknown>
): MessageInitShape<typeof EventSchema>["observation"] {
  return { case: "native", value: { direction, line: JSON.stringify(payload) } };
}

/**
 * Two turns, cited the way the runner cites: a derived event names the frame it was translated
 * from, while an input written to the harness and the harness's own noise name nothing. The
 * `session_raw` scenario reads them as one stream in sequence order — the stderr line between the
 * second turn's reasoning and its answer is where the ordering earns its keep.
 */
const EVENTS: Event[] = [
  event(1, { case: "harnessStarted", value: { resumed: false, pid: 7 } }),
  event(2, { case: "inputSubmitted", value: { inputId: "i1", text: "List the repository files." } }),
  event(3, frame(Direction.TO_HARNESS, { type: "user", text: "List the repository files." })),
  event(4, frame(Direction.FROM_HARNESS, { type: "turn.started" })),
  event(5, { case: "turnStarted", value: { turnId: "t1" } }, [4]),
  event(6, { case: "inputAccepted", value: { inputId: "i1", turnId: "t1" } }, [4]),
  event(7, frame(Direction.FROM_HARNESS, { type: "thinking", text: THINKING })),
  event(8, { case: "itemStarted", value: { itemId: "r#0", kind: ItemKind.REASONING } }, [7]),
  event(9, { case: "textDelta", value: { itemId: "r#0", text: THINKING } }, [7]),
  event(10, { case: "itemCompleted", value: { itemId: "r#0", outcome: { case: "text", value: THINKING } } }, [7]),
  event(11, frame(Direction.FROM_HARNESS, { type: "tool_use", name: "Bash", input: { command: "ls" } })),
  event(12, { case: "itemStarted", value: { itemId: "toolu_1", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }, [11]),
  event(13, { case: "toolArguments", value: { itemId: "toolu_1", argumentsJson: '{"command": "ls"}' } }, [11]),
  event(14, frame(Direction.FROM_HARNESS, { type: "tool_result", is_error: false })),
  event(
    15,
    {
      case: "itemCompleted",
      value: { itemId: "toolu_1", outcome: { case: "tool", value: { output: "README.md\nsrc\n", succeeded: true } } },
    },
    [14]
  ),
  event(16, frame(Direction.FROM_HARNESS, { type: "text", text: ANSWER })),
  event(17, { case: "itemStarted", value: { itemId: "m#0", kind: ItemKind.ASSISTANT_TEXT } }, [16]),
  event(18, { case: "textDelta", value: { itemId: "m#0", text: ANSWER } }, [16]),
  event(19, { case: "itemCompleted", value: { itemId: "m#0", outcome: { case: "text", value: ANSWER } } }, [16]),
  event(20, frame(Direction.FROM_HARNESS, { type: "turn.completed" })),
  event(21, { case: "turnCompleted", value: { turnId: "t1", status: TurnStatus.COMPLETED } }, [20]),
  event(22, { case: "inputSubmitted", value: { inputId: "i2", text: "Now read src." } }),
  event(23, frame(Direction.TO_HARNESS, { type: "user", text: "Now read src." })),
  event(24, frame(Direction.FROM_HARNESS, { type: "turn.started" })),
  event(25, { case: "turnStarted", value: { turnId: "t2" } }, [24]),
  event(26, { case: "inputAccepted", value: { inputId: "i2", turnId: "t2" } }, [24]),
  event(27, frame(Direction.FROM_HARNESS, { type: "thinking", text: THINKING_AGAIN })),
  event(28, { case: "itemStarted", value: { itemId: "r#1", kind: ItemKind.REASONING } }, [27]),
  event(29, { case: "textDelta", value: { itemId: "r#1", text: THINKING_AGAIN } }, [27]),
  event(
    30,
    { case: "itemCompleted", value: { itemId: "r#1", outcome: { case: "text", value: THINKING_AGAIN } } },
    [27]
  ),
  event(31, { case: "harnessStderr", value: { text: "warning: /state/work is not a git repository\n" } }),
  event(32, frame(Direction.FROM_HARNESS, { type: "text", text: "Reading `src` now" })),
  event(33, { case: "itemStarted", value: { itemId: "m#1", kind: ItemKind.ASSISTANT_TEXT } }, [32]),
  event(34, { case: "textDelta", value: { itemId: "m#1", text: "Reading `src` now" } }, [32]),
];

/**
 * A second canned script, not a second turn of the same conversation: every state the transcript
 * restyle (role-as-bubble, status-as-dot) touches that the main script above doesn't produce on
 * its own -- a failed tool call standing alone, a run whose reasoning is still streaming beside a
 * tool call that already failed, and a message still queued mid-turn. Not meant to read as a
 * plausible conversation; each piece exists to make one dot's rendering show up in a diff.
 */
const EVENTS_STATES: Event[] = [
  event(1, { case: "harnessStarted", value: { resumed: false, pid: 9 } }),
  event(2, { case: "inputSubmitted", value: { inputId: "i1", text: "Delete the stale branch." } }),
  event(3, { case: "turnStarted", value: { turnId: "t1" } }),
  event(4, { case: "inputAccepted", value: { inputId: "i1", turnId: "t1" } }),
  event(5, { case: "itemStarted", value: { itemId: "tool#0", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }),
  event(6, { case: "toolArguments", value: { itemId: "tool#0", argumentsJson: '{"command": "git branch -d stale"}' } }),
  event(7, {
    case: "itemCompleted",
    value: {
      itemId: "tool#0",
      outcome: { case: "tool", value: { output: "fatal: branch 'stale' not found.", succeeded: false } },
    },
  }),
  event(8, { case: "itemStarted", value: { itemId: "m#0", kind: ItemKind.ASSISTANT_TEXT } }),
  event(9, { case: "textDelta", value: { itemId: "m#0", text: "That branch doesn't exist." } }),
  event(10, {
    case: "itemCompleted",
    value: { itemId: "m#0", outcome: { case: "text", value: "That branch doesn't exist." } },
  }),
  event(11, { case: "turnCompleted", value: { turnId: "t1", status: TurnStatus.COMPLETED } }),
  event(12, {
    case: "inputSubmitted",
    value: { inputId: "i2", text: "Run the test suite twice, thinking it over first." },
  }),
  event(13, { case: "turnStarted", value: { turnId: "t2" } }),
  event(14, { case: "inputAccepted", value: { inputId: "i2", turnId: "t2" } }),
  event(15, { case: "itemStarted", value: { itemId: "r#0", kind: ItemKind.REASONING } }),
  event(16, {
    case: "textDelta",
    value: { itemId: "r#0", text: "Running it once could hide a flaky failure; twice tells the difference." },
  }),
  // r#0 never completes: the run it starts is still thinking while its own tool calls finish.
  event(17, { case: "itemStarted", value: { itemId: "tool#1", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }),
  event(18, { case: "toolArguments", value: { itemId: "tool#1", argumentsJson: '{"command": "bazel test //..."}' } }),
  event(19, {
    case: "itemCompleted",
    value: { itemId: "tool#1", outcome: { case: "tool", value: { output: "42 passed", succeeded: true } } },
  }),
  event(20, { case: "itemStarted", value: { itemId: "tool#2", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }),
  event(21, { case: "toolArguments", value: { itemId: "tool#2", argumentsJson: '{"command": "bazel test //..."}' } }),
  event(22, {
    case: "itemCompleted",
    value: { itemId: "tool#2", outcome: { case: "tool", value: { output: "1 test regressed", succeeded: false } } },
  }),
  // Turn t2 stays active: the run above (r#0, tool#1, tool#2) is what an in-progress, partly-failed
  // step looks like. i3 is never accepted: this is what a message queued mid-turn looks like.
  event(23, { case: "inputSubmitted", value: { inputId: "i3", text: "One more thing before you go." } }),
];

// Only what a page still asks for: the sandboxes, their bindings and their threads arrive on the
// live streams above.
routes.push(
  ["GET", /^\/models$/, () => ({ claude: ["harness-claude-model"], codex: ["harness-codex-model"] })],
  [
    "GET",
    /^\/presets$/,
    () => [
      {
        name: "public-coder",
        title: "Public coder",
        template: "agentplane-runner",
        policies: ["github-public"],
        thread_preset: "public-coder-codex",
        thread_defaults: {
          provider: "codex",
          model: "harness-codex-model",
          cwd: "/state/workspaces/{session_id}",
          reasoning_effort: "medium",
          instructions: "Work on public repositories only.",
        },
      },
    ],
  ],
  ["GET", /^\/egress\/policies$/, () => POLICIES],
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

/**
 * The app's two stream shapes: a live view, which is one snapshot and then whatever changes (here,
 * nothing), and a session, which is the canned turn and then silence, the way a session mid-turn
 * looks.
 */
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
    // The only other stream shape: a session's own events. Which script depends on which session
    // the URL names -- everything but `s-2` gets the original two-turn script above.
    const isStatesSession = url.pathname.endsWith(`/sessions/${ATTACHED_STATES.sessionId}/events`);
    const attached = isStatesSession ? ATTACHED_STATES : ATTACHED;
    const events = isStatesSession ? EVENTS_STATES : EVENTS;
    this.dispatchEvent(new MessageEvent("attached", { data: toJsonString(AttachedSchema, attached) }));
    for (const event of events) {
      this.dispatchEvent(
        new MessageEvent("event", { data: toJsonString(EventSchema, event), lastEventId: String(event.sequence) })
      );
    }
  }

  close(): void {
    this.readyState = 2;
  }
}

window.EventSource = HarnessEventSource as unknown as typeof EventSource;

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
if (scenario.openSettings) {
  // There's no dedicated route for the Settings modal; open it the way an operator would, by
  // clicking the nav button, rather than a URL that only exists for this test.
  const openSettings = new MutationObserver(() => {
    const button = [...document.querySelectorAll("button")].find((candidate) => candidate.textContent === "Settings");
    if (!button) return;
    openSettings.disconnect();
    button.click();
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
window.location.hash = scenario.route;

const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(
  <MantineProvider defaultColorScheme="auto" theme={{ fontFamily: "Inter, sans-serif" }}>
    <App />
  </MantineProvider>
);
