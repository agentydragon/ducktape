/**
 * Every visual scenario: the route the harness mounts, the fixture variations that route alone
 * does not express, and how the sweep captures it. One table, two readers -- harness.tsx routes
 * and seeds from it, visual/runner.mjs sweeps it -- so BUILD names no scenario at all.
 */

export interface Scenario {
  /** The app's hash route. The harness sets it before mounting, so App's router picks the view. */
  route: string;
  /**
   * The element to screenshot. Required, never defaulted: '#app' for a scenario that is genuinely
   * a full page (every one here is -- the harness mounts App and routes to it), a scenario-specific
   * selector for a single component, so its crop is its own bounding box. See
   * util/testing/frontend_visual/README.md § Screenshot target.
   */
  element: string;
  viewport: { width: number; height: number; deviceScaleFactor?: number };
  /** PNG stem for the published render. Defaults to the scenario's key. */
  outputName?: string;
  /** What must be on the page before it is this scene at all -- see runScenarios' docstring. */
  readySelectors?: string[];
  /** Screenshot the viewport rather than #app, preserving clipping instead of expanding to fit. */
  captureViewport?: boolean;
  /** Serve a watch that has stopped cycling: the staleness banner is the page saying so. */
  wedgedWatch?: boolean;
  /** Preselect an existing connection and service account: the reconnect review's opening state. */
  preselectReconnect?: boolean;
  /** Click the nav's Settings button once it mounts: the modal has no route of its own. */
  openSettings?: boolean;
}

/** A Pixel 6's CSS viewport: the app is used from a phone, so every page has to fit its width. */
const PHONE = { width: 412, height: 915, deviceScaleFactor: 2.625 };

const CONSENT_ROUTE = "/connection-enrollments/test-only-opaque-handle";
const SANDBOX_ROUTE = "/sandboxes/demo-a1b2";
const SESSION_ROUTE = `${SANDBOX_ROUTE}/sessions/s-1`;
// A standalone failed tool call, a run whose reasoning is still streaming beside a tool call that
// already failed, and a message queued mid-turn -- every status the session view's badge-to-dot
// restyle touches that the main `session` fixture doesn't produce on its own. The run's own
// open/closed state isn't URL-synced (unlike a reasoning block's), so it renders folded, which is
// fine here: its summary is exactly where the streaming/failed dots these scenarios exist for show.
const SESSION_STATES_ROUTE = `${SANDBOX_ROUTE}/sessions/s-2`;
// `%23` is the `#` of the item id: the view scrolls to the newest event, so the block to show open
// is the second turn's, and the first stays folded beside it.
const REASONING = "reasoning=r%231";

export const SCENARIOS: Record<string, Scenario> = {
  sandboxes: { element: "#app", route: "/", viewport: { width: 1200, height: 900 } },
  sandboxes_phone: { element: "#app", route: "/", viewport: PHONE, outputName: "sandboxes-phone" },
  sandboxes_stale: { element: "#app", route: "/", viewport: { width: 1200, height: 900 }, wedgedWatch: true },

  actions: { element: "#app", route: "/actions", viewport: { width: 1200, height: 1100 }, readySelectors: ["details"] },
  actions_phone: {
    element: "#app",
    route: "/actions",
    viewport: { width: 390, height: 1100 },
    readySelectors: ["details"],
  },
  actions_history: {
    element: "#app",
    route: "/actions/history",
    viewport: { width: 1200, height: 1400 },
    readySelectors: ["details"],
  },
  actions_history_phone: {
    element: "#app",
    route: "/actions/history",
    viewport: { width: 390, height: 1400 },
    readySelectors: ["details"],
  },

  connections: {
    element: "#app",
    route: "/",
    viewport: { width: 1200, height: 1000 },
    readySelectors: ["[data-connection-id]"],
    openSettings: true,
  },
  connections_phone: {
    element: "#app",
    route: "/",
    viewport: { width: 390, height: 1100 },
    readySelectors: ["[data-connection-id]"],
    openSettings: true,
  },

  consent: { element: "#app", route: CONSENT_ROUTE, viewport: { width: 1200, height: 1100 } },
  consent_phone: { element: "#app", route: CONSENT_ROUTE, viewport: { width: 390, height: 1100 } },
  consent_reconnect: {
    element: "#app",
    route: CONSENT_ROUTE,
    viewport: { width: 1200, height: 1300 },
    readySelectors: ["[data-reconnect-review]"],
    preselectReconnect: true,
  },
  consent_reconnect_phone: {
    element: "#app",
    route: CONSENT_ROUTE,
    viewport: { width: 390, height: 1600 },
    readySelectors: ["[data-reconnect-review]"],
    preselectReconnect: true,
  },

  sandbox: { element: "#app", route: SANDBOX_ROUTE, viewport: { width: 1200, height: 900 } },
  sandbox_phone: { element: "#app", route: SANDBOX_ROUTE, viewport: PHONE, outputName: "sandbox-phone" },
  // With the github-public binding's rules open, so the shot carries the credential detail -- its
  // description, where the proxy puts it, and which secret it comes from -- and the other binding,
  // still folded, shows the row the button starts as.
  sandbox_egress: {
    element: "#app",
    route: `${SANDBOX_ROUTE}?tab=egress&rules=demo-a1b2-github-public`,
    viewport: { width: 1200, height: 900 },
    outputName: "sandbox-egress",
  },
  sandbox_egress_phone: {
    element: "#app",
    route: `${SANDBOX_ROUTE}?tab=egress&rules=demo-a1b2-github-public`,
    viewport: PHONE,
    outputName: "sandbox-egress-phone",
  },

  session: { element: "#app", route: SESSION_ROUTE, viewport: { width: 1200, height: 900 }, captureViewport: true },
  session_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    outputName: "session-phone",
    captureViewport: true,
  },
  session_reasoning: {
    element: "#app",
    route: `${SESSION_ROUTE}?${REASONING}`,
    viewport: { width: 1200, height: 900 },
    outputName: "session-reasoning",
  },
  session_reasoning_phone: {
    element: "#app",
    route: `${SESSION_ROUTE}?${REASONING}`,
    viewport: PHONE,
    outputName: "session-reasoning-phone",
  },
  // The Raw frames switch on, so frames render beside the item, input and turn they were
  // translated into, and what belongs to none of them under "outside the transcript". Reasoning is
  // open too: a reader following the frames wants the thinking they produced. Taller than the
  // plain session scenario -- the transcript scrolls to the newest event and the stack is several
  // times as tall with every frame in it, so a 900-tall window ends inside the last item and the
  // review never sees a turn or an input take its place in the order.
  session_raw: {
    element: "#app",
    route: `${SESSION_ROUTE}?raw=1&${REASONING}`,
    viewport: { width: 1200, height: 1800 },
  },
  session_raw_phone: {
    element: "#app",
    route: `${SESSION_ROUTE}?raw=1&${REASONING}`,
    viewport: PHONE,
    outputName: "session-raw-phone",
  },
  session_states: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: { width: 1200, height: 900 },
    outputName: "session-states",
    captureViewport: true,
  },
  // The existing nav/header chrome (its own decluttering is separately tracked) leaves little
  // vertical room at phone width, so the queued-input dot at the bottom falls off the page -- same
  // as `session_raw_phone`, the desktop capture is where every state in this fixture is visible.
  session_states_phone: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: PHONE,
    outputName: "session-states-phone",
    captureViewport: true,
  },
};
