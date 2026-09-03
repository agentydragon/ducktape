/**
 * Visual-test harness: the app mounted on canned data, nothing on the network. The `?page=` query
 * (set by visual-test-lib) picks the route; `fetch` (stubbed by network.ts, imported first so the
 * app's client captures the stub) answers the inventory and session routes from fixtures, and
 * `EventSource` replays one turn of runner events into the session view.
 */
import "./network";
import "@mantine/core/styles.css";

import { create, toJson, toJsonString, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import App from "../app";
import type { BindingView, Decision, PolicyView, SandboxView, ThreadView } from "../client";
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
    profile: "coder",
    archived: false,
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
    profile: null,
    archived: false,
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
    profile: null,
    archived: false,
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
        credential: { secret: "harness-github-pat", key: "token", header: "Authorization" },
      },
    ],
  },
  {
    name: "pypi",
    rules: [
      { hosts: ["pypi.org", "files.pythonhosted.org"], methods: ["GET"], paths: ["/simple/**"], credential: null },
    ],
  },
];

/** One seed binding from git, active; one runtime ask still pending, which the proxy has refused so far. */
const BINDINGS: BindingView[] = [
  {
    name: "demo-a1b2-asks",
    granted_by: "agent",
    from_git: false,
    subjects: [{ sandbox: "demo-a1b2", match_labels: null }],
    approval: "pending",
    approved_by: null,
    approved_at: null,
    expires_at: new Date(NOW + 6 * HOUR).toISOString(),
    policies: [POLICIES[1]],
    missing_policies: [],
    active: false,
    active_reason: "NotApproved",
    active_message: "approval is pending",
  },
  {
    name: "sandboxes-github-public",
    granted_by: "flux",
    from_git: true,
    subjects: [{ sandbox: null, match_labels: { "agentplane.allegedly.works/managed": "true" } }],
    approval: "approved",
    approved_by: "harness-operator",
    approved_at: ago(24 * HOUR),
    expires_at: null,
    policies: [POLICIES[0]],
    missing_policies: [],
    active: true,
    active_reason: "Resolved",
    active_message: "1 of 1 policies resolved",
  },
];

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
    binding: "sandboxes-github-public",
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
    binding: "sandboxes-github-public",
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
    last_sequence: 31,
    last_event_at: ago(90 * 60_000),
  },
];

const ATTACHED: Attached = create(AttachedSchema, {
  sessionId: "s-1",
  spec: SPEC,
  lastSequence: 14n,
  harness: HarnessState.RUNNING,
});

function event(
  sequence: number,
  observation: MessageInitShape<typeof EventSchema>["observation"],
  sources: number[] = []
): Event {
  return create(EventSchema, { sequence: BigInt(sequence), observation, sourceSequences: sources.map(BigInt) });
}

/**
 * One finished turn and one mid-stream, cited the way the runner cites: a derived event names the
 * frame it was translated from, an input written to the harness names nothing, and the harness's
 * own noise (start, stderr) names nothing either. The `session_raw` scenario reads the citations —
 * frames beside their item, their input and their turn, and what is left over under "outside the
 * transcript".
 */
const EVENTS: Event[] = [
  event(1, { case: "harnessStarted", value: { resumed: false, pid: 7 } }),
  event(2, { case: "inputSubmitted", value: { inputId: "i1", text: "List the repository files." } }),
  event(3, {
    case: "native",
    value: { direction: Direction.TO_HARNESS, line: '{"type":"user","text":"List the repository files."}' },
  }),
  event(4, { case: "native", value: { direction: Direction.FROM_HARNESS, line: '{"type":"turn.started"}' } }),
  event(5, { case: "turnStarted", value: { turnId: "t1" } }, [4]),
  event(6, { case: "inputAccepted", value: { inputId: "i1", turnId: "t1" } }, [4]),
  event(7, {
    case: "native",
    value: { direction: Direction.FROM_HARNESS, line: '{"type":"thinking","text":"The user wants the files listed."}' },
  }),
  event(8, { case: "itemStarted", value: { itemId: "r#0", kind: ItemKind.REASONING } }, [7]),
  event(9, { case: "textDelta", value: { itemId: "r#0", text: "The user wants the files listed." } }, [7]),
  event(
    10,
    {
      case: "itemCompleted",
      value: { itemId: "r#0", outcome: { case: "text", value: "The user wants the files listed." } },
    },
    [7]
  ),
  event(11, {
    case: "native",
    value: { direction: Direction.FROM_HARNESS, line: '{"type":"tool_use","name":"Bash","input":{"command":"ls"}}' },
  }),
  event(12, { case: "itemStarted", value: { itemId: "toolu_1", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }, [11]),
  event(13, { case: "toolArguments", value: { itemId: "toolu_1", argumentsJson: '{"command": "ls"}' } }, [11]),
  event(14, { case: "harnessStderr", value: { text: "warning: /state/work is not a git repository\n" } }),
  event(15, {
    case: "native",
    value: { direction: Direction.FROM_HARNESS, line: '{"type":"tool_result","is_error":false}' },
  }),
  event(
    16,
    {
      case: "itemCompleted",
      value: { itemId: "toolu_1", outcome: { case: "tool", value: { output: "README.md\nsrc\n", succeeded: true } } },
    },
    [15]
  ),
  event(17, {
    case: "native",
    value: { direction: Direction.FROM_HARNESS, line: '{"type":"text","text":"Two entries: README.md and src."}' },
  }),
  event(18, { case: "itemStarted", value: { itemId: "m#0", kind: ItemKind.ASSISTANT_TEXT } }, [17]),
  event(19, { case: "textDelta", value: { itemId: "m#0", text: "Two entries: README.md and src." } }, [17]),
  event(
    20,
    {
      case: "itemCompleted",
      value: { itemId: "m#0", outcome: { case: "text", value: "Two entries: README.md and src." } },
    },
    [17]
  ),
  event(21, { case: "native", value: { direction: Direction.FROM_HARNESS, line: '{"type":"turn.completed"}' } }),
  event(22, { case: "turnCompleted", value: { turnId: "t1", status: TurnStatus.COMPLETED } }, [21]),
  event(23, { case: "inputSubmitted", value: { inputId: "i2", text: "Now read src." } }),
  event(24, {
    case: "native",
    value: { direction: Direction.TO_HARNESS, line: '{"type":"user","text":"Now read src."}' },
  }),
  event(25, { case: "native", value: { direction: Direction.FROM_HARNESS, line: '{"type":"turn.started"}' } }),
  event(26, { case: "turnStarted", value: { turnId: "t2" } }, [25]),
  event(27, { case: "inputAccepted", value: { inputId: "i2", turnId: "t2" } }, [25]),
  event(28, {
    case: "native",
    value: { direction: Direction.FROM_HARNESS, line: '{"type":"text","text":"Reading src now"}' },
  }),
  event(29, { case: "itemStarted", value: { itemId: "m#1", kind: ItemKind.ASSISTANT_TEXT } }, [28]),
  event(30, { case: "textDelta", value: { itemId: "m#1", text: "Reading src now" } }, [28]),
];

routes.push(
  ["GET", /^\/models$/, () => ({ claude: ["harness-claude-model"], codex: ["harness-codex-model"] })],
  ["GET", /^\/egress\/policies$/, () => POLICIES],
  ["GET", /^\/sandboxes$/, () => SANDBOXES],
  ["GET", /^\/sandboxes\/([^/]+)$/, (match) => SANDBOXES.find((row) => row.name === match[1])],
  ["GET", /^\/sandboxes\/([^/]+)\/egress$/, () => BINDINGS],
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

/** One attached stream: the canned events, then silence, the way a session mid-turn looks. */
class ReplayingEventSource extends EventTarget {
  readonly url: string;
  readyState = 1;

  constructor(url: string) {
    super();
    this.url = url;
    // After the view's listeners are attached, which happens right after construction.
    setTimeout(() => {
      this.dispatchEvent(new MessageEvent("attached", { data: toJsonString(AttachedSchema, ATTACHED) }));
      for (const event of EVENTS) {
        this.dispatchEvent(
          new MessageEvent("event", { data: toJsonString(EventSchema, event), lastEventId: String(event.sequence) })
        );
      }
    }, 0);
  }

  close(): void {
    this.readyState = 2;
  }
}

window.EventSource = ReplayingEventSource as unknown as typeof EventSource;

const PAGES: Record<string, string> = {
  sandboxes: "/",
  sandbox: "/sandboxes/demo-a1b2",
  sandbox_egress: "/sandboxes/demo-a1b2?tab=egress",
  session: "/sandboxes/demo-a1b2/sessions/s-1",
  session_raw: "/sandboxes/demo-a1b2/sessions/s-1?raw=1",
};

const page = new URLSearchParams(window.location.search).get("page") ?? "sandboxes";
const path = PAGES[page];
if (path === undefined) throw new Error(`unknown harness page ${page}`);
window.location.hash = path;

const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(
  <MantineProvider defaultColorScheme="auto" theme={{ fontFamily: "Inter, sans-serif" }}>
    <App />
  </MantineProvider>
);
