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

/** One seed binding from git, which only git removes; one the app granted at launch, now expired. */
const BINDINGS: BindingView[] = [
  {
    name: "demo-a1b2-picked",
    granted_by: "agent",
    from_git: false,
    subjects: ["demo-a1b2"],
    expires_at: ago(2 * HOUR),
    policies: [POLICIES[1]],
    missing_policies: [],
    active: false,
    active_reason: "Expired",
    active_message: "1 of 1 policies resolved",
  },
  {
    name: "demo-a1b2-github-public",
    granted_by: "flux",
    from_git: true,
    subjects: ["demo-a1b2"],
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
  session_reasoning: "/sandboxes/demo-a1b2/sessions/s-1?reasoning=open",
  // The two switches are independent parameters, and the raw scenario turns both on: a reader
  // following the frames wants the thinking they produced open too.
  session_raw: "/sandboxes/demo-a1b2/sessions/s-1?raw=1&reasoning=open",
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
