/**
 * Visual-test harness: the app mounted on canned data, nothing on the network. The `?page=` query
 * (set by visual-test-lib) picks the route; `fetch` (stubbed by network.ts, imported first so the
 * app's client captures the stub) answers the inventory and session routes from fixtures, and
 * `EventSource` replays one turn of runner events into the session view.
 */
import "./network";
import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import App from "../app";
import type { Attached, Event, SandboxView, SessionSpec, SessionSummary } from "../client";
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
    model: "harness-model-cheap",
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
    model: "harness-model-cheap",
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
    model: "harness-model-cheap",
    archived: false,
    state: "suspended",
    created_at: ago(48 * HOUR),
    operating_mode: "Suspended",
    conditions: [{ type: "Ready", status: "False", reason: "Suspended", message: null }],
    node_name: null,
    pod: null,
  },
];

const SPEC: SessionSpec = {
  provider: "PROVIDER_CLAUDE",
  cwd: "/state/work",
  model: "harness-model-cheap",
  reasoningEffort: "low",
};

const SESSIONS: SessionSummary[] = [
  { sessionId: "s-1", spec: SPEC, lastSequence: "14", harness: "HARNESS_STATE_RUNNING", activeTurnId: "" },
  { sessionId: "s-0", spec: SPEC, lastSequence: "31", harness: "HARNESS_STATE_STOPPED", activeTurnId: "" },
];

const ATTACHED: Attached = { sessionId: "s-1", spec: SPEC, lastSequence: "14", harness: "HARNESS_STATE_RUNNING" };

const EVENTS: Event[] = [
  { sequence: "1", harnessStarted: { resumed: false, pid: 7 } },
  { sequence: "2", inputSubmitted: { inputId: "i1" } },
  { sequence: "3", turnStarted: { turnId: "t1" } },
  { sequence: "4", inputAccepted: { inputId: "i1", turnId: "t1" } },
  { sequence: "5", itemStarted: { itemId: "r#0", kind: "ITEM_KIND_REASONING" } },
  { sequence: "6", textDelta: { itemId: "r#0", text: "The user wants the files listed." } },
  { sequence: "7", itemCompleted: { itemId: "r#0", text: "The user wants the files listed." } },
  { sequence: "8", itemStarted: { itemId: "toolu_1", kind: "ITEM_KIND_TOOL_CALL", toolName: "Bash" } },
  { sequence: "9", toolArguments: { itemId: "toolu_1", argumentsJson: '{"command": "ls"}' } },
  { sequence: "10", native: { direction: "DIRECTION_FROM_HARNESS", line: '{"type":"tool_use","name":"Bash"}' } },
  { sequence: "11", itemCompleted: { itemId: "toolu_1", tool: { output: "README.md\nsrc\n", succeeded: true } } },
  { sequence: "12", itemStarted: { itemId: "m#0", kind: "ITEM_KIND_ASSISTANT_TEXT" } },
  { sequence: "13", textDelta: { itemId: "m#0", text: "Two entries: README.md and src." } },
  { sequence: "14", itemCompleted: { itemId: "m#0", text: "Two entries: README.md and src." } },
  { sequence: "15", turnCompleted: { turnId: "t1", status: "TURN_STATUS_COMPLETED" } },
  { sequence: "16", inputSubmitted: { inputId: "i2" } },
  { sequence: "17", turnStarted: { turnId: "t2" } },
  { sequence: "18", inputAccepted: { inputId: "i2", turnId: "t2" } },
  { sequence: "19", itemStarted: { itemId: "m#1", kind: "ITEM_KIND_ASSISTANT_TEXT" } },
  { sequence: "20", textDelta: { itemId: "m#1", text: "Reading src now" } },
];

routes.push(
  ["GET", /^\/sandboxes$/, () => SANDBOXES],
  ["GET", /^\/sandboxes\/([^/]+)$/, (match) => SANDBOXES.find((row) => row.name === match[1])],
  ["GET", /^\/sandboxes\/([^/]+)\/sessions$/, () => SESSIONS]
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
      this.dispatchEvent(new MessageEvent("attached", { data: JSON.stringify(ATTACHED) }));
      for (const event of EVENTS) {
        this.dispatchEvent(new MessageEvent("event", { data: JSON.stringify(event), lastEventId: event.sequence }));
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
