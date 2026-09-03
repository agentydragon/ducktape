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
import type { SandboxView } from "../client";
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
    provider: "claude",
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
    provider: "codex",
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
    provider: "claude",
    archived: false,
    state: "suspended",
    created_at: ago(48 * HOUR),
    operating_mode: "Suspended",
    conditions: [{ type: "Ready", status: "False", reason: "Suspended", message: null }],
    node_name: null,
    pod: null,
  },
];

const SPEC: SessionSpec = create(SessionSpecSchema, {
  provider: Provider.CLAUDE,
  cwd: "/state/work",
  reasoningEffort: "low",
});

const SESSIONS: SessionSummary[] = [
  create(SessionSummarySchema, { sessionId: "s-1", spec: SPEC, lastSequence: 14n, harness: HarnessState.RUNNING }),
  create(SessionSummarySchema, { sessionId: "s-0", spec: SPEC, lastSequence: 31n, harness: HarnessState.STOPPED }),
];

const ATTACHED: Attached = create(AttachedSchema, {
  sessionId: "s-1",
  spec: SPEC,
  lastSequence: 14n,
  harness: HarnessState.RUNNING,
});

function event(sequence: number, observation: MessageInitShape<typeof EventSchema>["observation"]): Event {
  return create(EventSchema, { sequence: BigInt(sequence), observation });
}

const EVENTS: Event[] = [
  event(1, { case: "harnessStarted", value: { resumed: false, pid: 7 } }),
  event(2, { case: "inputSubmitted", value: { inputId: "i1" } }),
  event(3, { case: "turnStarted", value: { turnId: "t1" } }),
  event(4, { case: "inputAccepted", value: { inputId: "i1", turnId: "t1" } }),
  event(5, { case: "itemStarted", value: { itemId: "r#0", kind: ItemKind.REASONING } }),
  event(6, { case: "textDelta", value: { itemId: "r#0", text: "The user wants the files listed." } }),
  event(7, {
    case: "itemCompleted",
    value: { itemId: "r#0", outcome: { case: "text", value: "The user wants the files listed." } },
  }),
  event(8, { case: "itemStarted", value: { itemId: "toolu_1", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }),
  event(9, { case: "toolArguments", value: { itemId: "toolu_1", argumentsJson: '{"command": "ls"}' } }),
  event(10, {
    case: "native",
    value: { direction: Direction.FROM_HARNESS, line: '{"type":"tool_use","name":"Bash"}' },
  }),
  event(11, {
    case: "itemCompleted",
    value: { itemId: "toolu_1", outcome: { case: "tool", value: { output: "README.md\nsrc\n", succeeded: true } } },
  }),
  event(12, { case: "itemStarted", value: { itemId: "m#0", kind: ItemKind.ASSISTANT_TEXT } }),
  event(13, { case: "textDelta", value: { itemId: "m#0", text: "Two entries: README.md and src." } }),
  event(14, {
    case: "itemCompleted",
    value: { itemId: "m#0", outcome: { case: "text", value: "Two entries: README.md and src." } },
  }),
  event(15, { case: "turnCompleted", value: { turnId: "t1", status: TurnStatus.COMPLETED } }),
  event(16, { case: "inputSubmitted", value: { inputId: "i2" } }),
  event(17, { case: "turnStarted", value: { turnId: "t2" } }),
  event(18, { case: "inputAccepted", value: { inputId: "i2", turnId: "t2" } }),
  event(19, { case: "itemStarted", value: { itemId: "m#1", kind: ItemKind.ASSISTANT_TEXT } }),
  event(20, { case: "textDelta", value: { itemId: "m#1", text: "Reading src now" } }),
];

routes.push(
  ["GET", /^\/models$/, () => ({ claude: ["harness-claude-model"], codex: ["harness-codex-model"] })],
  ["GET", /^\/sandboxes$/, () => SANDBOXES],
  ["GET", /^\/sandboxes\/([^/]+)$/, (match) => SANDBOXES.find((row) => row.name === match[1])],
  ["GET", /^\/sandboxes\/([^/]+)\/sessions$/, () => SESSIONS.map((session) => toJson(SessionSummarySchema, session))]
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
  session: "/sandboxes/demo-a1b2/sessions/s-1",
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
