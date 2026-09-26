/**
 * Every visual scenario: the route the harness mounts, the fixture variations that route alone
 * does not express, and how the sweep captures it. One table, two readers -- harness.tsx routes
 * and seeds from it, visual/runner.mjs sweeps it -- so BUILD names no scenario at all.
 */

import type { ScenarioOptions, Viewport } from "../../../../util/testing/frontend_visual/visual-test-lib.mjs";

/**
 * A scenario is how the sweep captures it plus what this harness needs to build it. The capture
 * half comes from the library rather than being restated here, so a field the sweep does not read
 * fails to compile instead of silently doing nothing; `element` stays required from there.
 */
export interface Scenario extends ScenarioOptions {
  /** The app's hash route. The harness sets it before mounting, so App's router picks the view. */
  route: string;
  /** Required here, unlike the library's default, because every row states the size it needs. */
  viewport: Viewport;
  /** Serve a watch that has stopped cycling: the staleness banner is the page saying so. */
  wedgedWatch?: boolean;
  /** Preselect an existing connection and service account: the reconnect review's opening state. */
  preselectReconnect?: boolean;
  /** Click the nav's Settings button once it mounts: the modal has no route of its own. */
  openSettings?: boolean;
  /** Flip every Raw switch as it mounts: no URL param toggles one. */
  openRaw?: boolean;
  /** Once the preset's pick has landed as a pill, open the action policy sets dropdown. */
  openActionPolicySets?: boolean;
  /** Click the phone-width hamburger once it mounts: the sidebar drawer has no route of its own
   * (UISHELL_MOBILE). */
  openMobileSidebar?: boolean;
  threadlessSandbox?: boolean;
  /** `disconnected` drops the sidebar's own stream after its first snapshot; `database-disconnected`
   * keeps it up but reports the server's database feed down. */
  sidebarSource?: "disconnected" | "database-disconnected";
  /** Drop the sandbox inventory stream after its first snapshot, leaving the sidebar's own stream
   * up. */
  inventoryDropped?: boolean;
  /** How long, in ms, the streams this scenario drops have been down when it renders. Without it
   * they have only just dropped, which shows nothing. */
  outageAge?: number;
  /** Focus the sidebar's connection indicator once it shows, which opens its tooltip. */
  openConnectionStatus?: boolean;
  /** Exercise the production scope and Electric shape synchronization boundary. `unavailable`
   * is a persistent initial service failure, unlike a retired epoch, whose 410 triggers a refresh;
   * `reconnecting` fails every live read of the thread's rows once they have loaded, which
   * Electric's client retries. */
  sessionReplay?: "catching-up" | "unavailable" | "reconnecting";
  /** Assistant output precedes coalesced queued input, then model/interrupt effects. */
  interleavedEvents?: boolean;
  /** Open the chronological archive drawer, the native-frame inspection surface. */
  openDebug?: "latest" | "stderr";
  /** Open the tool-call run once it mounts, then the reasoning step folded inside it. */
  openReasoning?: boolean;
  /** Open the tool-call run once it mounts, then the tool call's Arguments and Output inside it. */
  openToolPayloads?: boolean;
  /** Click the Evidence icon of the row at this thread anchor once it mounts: which rows show
   * their evidence is not in the URL. */
  openEvidence?: string;
  pendingCommands?: "mixed" | "controls" | "outcomes";
  /** Answer a command POST as the app does when a runner misses its admission deadline. Without
   * this it stays unanswered, like one queued behind the browser's connection limit. */
  commandAdmissionTimedOut?: boolean;
  /** Fail the Action group listing, which says whether a stored result is an MCP `CallToolResult`. */
  actionGroupsUnavailable?: boolean;
  failedTurn?: "before-content" | "after-content";
}

/** A Pixel 6's CSS viewport: the app is used from a phone, so every page has to fit its width. */
const PHONE = { width: 412, height: 915, deviceScaleFactor: 2.625 };

const CONSENT_ROUTE = "/connection-enrollments/test-only-opaque-handle";
const SANDBOX_ROUTE = "/sandboxes/demo-a1b2";
const SESSION_ROUTE = "/threads/5f1c4a2e-0000-4000-8000-000000000001";
// A standalone failed tool call, a run whose reasoning is still streaming beside a tool call, and
// queued commands -- statuses the main `session` fixture doesn't produce on its own. Both runs
// render folded, which is the point here: a run's summary is where its streaming and failed
// indicators show. The run's first step, at cursor 16, is its anchor.
const SESSION_STATES_ROUTE = "/threads/5f1c4a2e-0000-4000-8000-000000000002";
// Threads on the fixture's suspended sandbox, and on one the inventory does not list.
const SUSPENDED_SANDBOX_SESSION_ROUTE = "/threads/5f1c4a2e-0000-4000-8000-000000000004";
const DELETED_SANDBOX_SESSION_ROUTE = "/threads/5f1c4a2e-0000-4000-8000-000000000005";
export const SCENARIOS: Record<string, Scenario> = {
  session_error: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    failedTurn: "before-content",
    readySelectors: ['[data-thread-anchor="6"]'],
    captureViewport: true,
  },
  session_error_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    failedTurn: "after-content",
    readySelectors: ['[data-thread-anchor="8"]'],
    captureViewport: true,
  },
  session_error_raw: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 1100 },
    failedTurn: "after-content",
    openDebug: "latest",
    readySelectors: ['[aria-label="Chronological observations"]'],
    captureViewport: true,
  },
  session_error_raw_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    failedTurn: "before-content",
    openDebug: "latest",
    readySelectors: ['[aria-label="Chronological observations"]'],
    captureViewport: true,
  },
  session_interleaved: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 1100 },
    interleavedEvents: true,
    readySelectors: ['[data-thread-anchor="18"]'],
    captureViewport: true,
  },
  session_interleaved_raw: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 1500 },
    interleavedEvents: true,
    openDebug: "latest",
    readySelectors: ['[aria-label="Chronological observations"]'],
    captureViewport: true,
  },
  session_interleaved_raw_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    interleavedEvents: true,
    openDebug: "latest",
    readySelectors: ['[aria-label="Chronological observations"]'],
    captureViewport: true,
  },
  session_interleaved_native_details: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 1100 },
    interleavedEvents: true,
    openDebug: "stderr",
    readySelectors: ['[aria-label="Chronological observations"]'],
    captureViewport: true,
  },
  // The sidebar's landing state (UISHELL_SIDEBAR): every group state icon (running, pending,
  // suspended, deleted) and the struck-through read-only group, with no thread open yet.
  threads: {
    element: "#app",
    route: "/",
    viewport: { width: 1200, height: 900 },
    readySelectors: ["a.agentplane-sidebar-group-name"],
  },
  threads_phone: { element: "#app", route: "/", viewport: PHONE, outputName: "threads-phone" },
  // The phone-width sidebar drawer opened over the landing view (UISHELL_MOBILE): the hamburger,
  // the backdrop, and the same group/thread list the desktop sidebar shows.
  threads_phone_drawer: {
    element: "#app",
    route: "/",
    viewport: PHONE,
    outputName: "threads-phone-drawer",
    readySelectors: [".agentplane-sidebar-backdrop", "a.agentplane-sidebar-group-name"],
    openMobileSidebar: true,
  },
  threads_provisioning: {
    element: "#app",
    route: "/",
    viewport: { width: 1200, height: 900 },
    threadlessSandbox: true,
    readySelectors: ['a[href="#/sandboxes/test-provisioning"]'],
  },
  threads_provisioning_phone: {
    element: "#app",
    route: "/",
    viewport: PHONE,
    threadlessSandbox: true,
    openMobileSidebar: true,
    readySelectors: ['a[href="#/sandboxes/test-provisioning"]', ".agentplane-sidebar-backdrop"],
  },
  threads_updates_disconnected: {
    element: "#app",
    route: "/",
    viewport: { width: 1200, height: 900 },
    sidebarSource: "database-disconnected",
    readySelectors: ['[role="alert"]'],
  },
  // The sidebar's own stream down past the grace: the footer's spinner, its tooltip naming the stream.
  threads_disconnected: {
    element: "#app",
    route: "/",
    viewport: { width: 1200, height: 900 },
    sidebarSource: "disconnected",
    outageAge: 10_000,
    openConnectionStatus: true,
    readySelectors: ['[data-connection="degraded"]', "::-p-text(Threads: reconnecting since)"],
    captureViewport: true,
  },
  threads_disconnected_phone: {
    element: "#app",
    route: "/",
    viewport: PHONE,
    sidebarSource: "disconnected",
    outageAge: 10_000,
    openMobileSidebar: true,
    readySelectors: [".agentplane-sidebar-backdrop", '[data-connection="degraded"]'],
  },
  threads_watch_stale: {
    element: "#app",
    route: "/",
    viewport: { width: 1200, height: 900 },
    wedgedWatch: true,
    readySelectors: ['[role="alert"]'],
  },

  sandboxes: { element: "#app", route: "/sandboxes", viewport: { width: 1200, height: 900 } },
  sandboxes_phone: { element: "#app", route: "/sandboxes", viewport: PHONE, outputName: "sandboxes-phone" },
  sandboxes_stale: {
    element: "#app",
    route: "/sandboxes",
    viewport: { width: 1200, height: 900 },
    wedgedWatch: true,
  },
  // The launch form with a preset picked through the URL and the sets dropdown opened by the
  // harness, so the shot carries the namespace's options beside the pre-filled pick. `/sandboxes`,
  // not `/`: UISHELL_SIDEBAR moved the Sandbox list off the landing route.
  new_sandbox: {
    element: "#app",
    route: "/sandboxes?preset=public-coder",
    viewport: { width: 1200, height: 900 },
    outputName: "new-sandbox",
    readySelectors: [".mantine-Pill-root", '[role="listbox"]'],
    openActionPolicySets: true,
  },
  new_sandbox_phone: {
    element: "#app",
    route: "/sandboxes?preset=public-coder",
    viewport: PHONE,
    outputName: "new-sandbox-phone",
    readySelectors: [".mantine-Pill-root", '[role="listbox"]'],
    openActionPolicySets: true,
  },

  actions: { element: "#app", route: "/actions", viewport: { width: 1200, height: 1100 }, readySelectors: ["details"] },
  actions_phone: {
    element: "#app",
    route: "/actions",
    viewport: { width: 390, height: 1100 },
    readySelectors: ["details"],
  },
  // Each pending card that draws anything other than its JSON switched to Raw.
  actions_raw: {
    element: "#app",
    route: "/actions",
    viewport: { width: 1200, height: 1100 },
    readySelectors: ["details", 'input[type="checkbox"]:checked'],
    openRaw: true,
  },
  // The image is an MCP result drawn as the tool answered, which also waits on the Action groups.
  // Each history viewport is tall enough to keep the last card in frame.
  actions_history: {
    element: "#app",
    route: "/actions/history",
    viewport: { width: 1200, height: 2560 },
    readySelectors: ["details", 'img[src^="data:image/"]'],
  },
  actions_history_phone: {
    element: "#app",
    route: "/actions/history",
    viewport: { width: 390, height: 3200 },
    readySelectors: ["details", 'img[src^="data:image/"]'],
  },
  // Each card that draws anything other than its JSON switched to Raw: the stored arguments and
  // CallToolResult, image data and all.
  actions_history_raw: {
    element: "#app",
    route: "/actions/history",
    viewport: { width: 1200, height: 3720 },
    readySelectors: ["details", 'input[type="checkbox"]:checked'],
    openRaw: true,
  },
  // Without the groups nothing says which results are MCP ones: each shows as its stored JSON.
  actions_history_groups_unavailable: {
    element: "#app",
    route: "/actions/history",
    viewport: { width: 1200, height: 3760 },
    actionGroupsUnavailable: true,
    readySelectors: ["details", '[role="alert"]'],
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
  // Where the MCP-linkage OAuth callback lands: Settings, open on its MCP servers tab.
  mcp_servers: {
    element: "#app",
    route: "/mcp-servers",
    viewport: { width: 1200, height: 1480 },
    readySelectors: ["[data-mcp-server]"],
  },
  mcp_servers_phone: {
    element: "#app",
    route: "/mcp-servers",
    viewport: { width: 390, height: 2040 },
    readySelectors: ["[data-mcp-server]"],
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
  // The Status tab's Raw switch on, so the shot carries the syntax-highlighted whole-Sandbox JSON
  // dump rather than only the summarized view every other sandbox scenario shows.
  sandbox_status_raw: {
    element: "#app",
    route: `${SANDBOX_ROUTE}?tab=status`,
    viewport: { width: 1200, height: 900 },
    outputName: "sandbox-status-raw",
    readySelectors: [".agentplane-hljs"],
    openRaw: true,
  },
  sandbox_status_raw_phone: {
    element: "#app",
    route: `${SANDBOX_ROUTE}?tab=status`,
    viewport: PHONE,
    outputName: "sandbox-status-raw-phone",
    readySelectors: [".agentplane-hljs"],
    openRaw: true,
  },
  // The read-only action policy: both bindings, every set state, and the three lists.
  sandbox_policy: {
    element: "#app",
    route: `${SANDBOX_ROUTE}?tab=policy`,
    viewport: { width: 1200, height: 1100 },
    outputName: "sandbox-policy",
  },
  sandbox_policy_phone: {
    element: "#app",
    route: `${SANDBOX_ROUTE}?tab=policy`,
    viewport: PHONE,
    outputName: "sandbox-policy-phone",
  },

  session: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    readySelectors: ['[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  session_deleted_sandbox: {
    element: "#app",
    route: DELETED_SANDBOX_SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    readySelectors: ['[role="status"]'],
    captureViewport: true,
  },
  session_deleted_sandbox_phone: {
    element: "#app",
    route: DELETED_SANDBOX_SESSION_ROUTE,
    viewport: PHONE,
    readySelectors: ['[role="status"]'],
    captureViewport: true,
  },
  session_suspended_sandbox: {
    element: "#app",
    route: SUSPENDED_SANDBOX_SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    readySelectors: ["::-p-text(Last observed Sandbox state)", '[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  session_suspended_sandbox_phone: {
    element: "#app",
    route: SUSPENDED_SANDBOX_SESSION_ROUTE,
    viewport: PHONE,
    readySelectors: ["::-p-text(Last observed Sandbox state)", '[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  // The sandbox inventory the controls wait on has stopped moving, on a running sandbox: the header's
  // banner, beside the sidebar's for the same watch, is all that says why the composer is off.
  session_inventory_stale: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    wedgedWatch: true,
    readySelectors: ["::-p-text(so this page is not being updated)", '[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  // A stale inventory that lacks the thread's sandbox cannot say it was deleted.
  session_inventory_stale_phone: {
    element: "#app",
    route: DELETED_SANDBOX_SESSION_ROUTE,
    viewport: PHONE,
    wedgedWatch: true,
    readySelectors: ["::-p-text(Current availability unknown)", '[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  // The inventory the thread's controls wait on has been down a minute: the page's notice, and the
  // sidebar's spinner gone amber.
  session_inventory_dropped: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    inventoryDropped: true,
    outageAge: 90_000,
    readySelectors: ['[data-connection="stale"]', "::-p-text(may be out of date)", '[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  session_inventory_dropped_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    inventoryDropped: true,
    outageAge: 90_000,
    readySelectors: ["::-p-text(may be out of date)", '[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  session_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    outputName: "session-phone",
    readySelectors: ['[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  // An unnamed thread: the title field shows the thread id as its placeholder and nothing beside it.
  session_unnamed: {
    element: "#app",
    route: "/threads/5f1c4a2e-0000-4000-8000-000000000000",
    viewport: { width: 1200, height: 900 },
    readySelectors: ['[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  session_unnamed_phone: {
    element: "#app",
    route: "/threads/5f1c4a2e-0000-4000-8000-000000000000",
    viewport: PHONE,
    readySelectors: ['[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  session_reasoning: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    outputName: "session-reasoning",
    openReasoning: true,
    readySelectors: ["details[open] details[open] .agentplane-markdown"],
  },
  session_reasoning_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    outputName: "session-reasoning-phone",
    openReasoning: true,
    readySelectors: ["details[open] details[open] .agentplane-markdown"],
  },
  // JSON arguments highlighted, and a non-JSON output in the same code block, uninterpreted. The
  // history follows its bottom, so the viewports are tall enough to keep the tool call, and the
  // user's input above it, on screen.
  session_tool_payloads: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 1300 },
    outputName: "session-tool-payloads",
    openToolPayloads: true,
    readySelectors: [".agentplane-hljs .hljs-attr", "details[open] + details[open] .agentplane-hljs"],
  },
  session_tool_payloads_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { ...PHONE, height: 1500 },
    outputName: "session-tool-payloads-phone",
    openToolPayloads: true,
    readySelectors: [".agentplane-hljs .hljs-attr", "details[open] + details[open] .agentplane-hljs"],
  },
  // A row's evidence opened from its corner icon: on the user bubble, the one card with no header
  // row to hold the icon, and at phone width in the last reply's header row.
  session_evidence: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 1100 },
    openEvidence: "4",
    readySelectors: ['[data-thread-anchor="4"] [data-evidence-observation]', '[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  session_evidence_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    openEvidence: "34",
    readySelectors: ['[data-thread-anchor="34"] [data-evidence-observation]'],
    captureViewport: true,
  },
  // Native observations are inspected through the chronological drawer. The projected view has
  // no raw-event URL mode: its semantic entities stay identical while the drawer shows archive rows.
  session_raw: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 1800 },
    openDebug: "latest",
    readySelectors: ['[aria-label="Chronological observations"]'],
  },
  session_raw_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    outputName: "session-raw-phone",
    openDebug: "latest",
    readySelectors: ['[aria-label="Chronological observations"]'],
  },
  session_states: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: { width: 1200, height: 900 },
    outputName: "session-states",
    readySelectors: ['[data-thread-anchor="16"]'],
    captureViewport: true,
  },
  session_pending: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: { width: 1200, height: 1100 },
    pendingCommands: "mixed",
    readySelectors: ['[aria-label="Pending commands"]', '[data-thread-anchor="16"]'],
    captureViewport: true,
  },
  session_pending_phone: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: PHONE,
    pendingCommands: "mixed",
    readySelectors: ['[aria-label="Pending commands"]', '[data-thread-anchor="16"]'],
    captureViewport: true,
  },
  session_pending_raw: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: { width: 1200, height: 1100 },
    pendingCommands: "mixed",
    openDebug: "latest",
    readySelectors: ['[aria-label="Chronological observations"]', '[data-thread-anchor="16"]'],
    captureViewport: true,
  },
  session_pending_failed: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: { width: 1200, height: 1100 },
    pendingCommands: "mixed",
    commandAdmissionTimedOut: true,
    readySelectors: ["::-p-text(runner did not admit the command)", '[data-thread-anchor="16"]'],
    captureViewport: true,
  },
  session_pending_controls: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: { width: 1200, height: 1100 },
    pendingCommands: "controls",
    readySelectors: ['[data-command-id="queued-interrupt"]', '[data-thread-anchor="16"]'],
    captureViewport: true,
  },
  session_command_outcomes_phone: {
    element: "#app",
    route: SESSION_STATES_ROUTE,
    viewport: PHONE,
    pendingCommands: "outcomes",
    readySelectors: ['[aria-label="Pending commands"]', '[data-thread-anchor="16"]'],
    captureViewport: true,
  },
  session_catching_up: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    sessionReplay: "catching-up",
    // The intentionally stale view state withholds segments while it catches up.
    readySelectors: ['[data-thread-catchup="true"]'],
  },
  session_sync_unavailable: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    sessionReplay: "unavailable",
    readySelectors: ['[role="alert"]'],
  },
  // The rows stay on screen while the client retries. Past the grace the composer's dot and the
  // sidebar's spinner say so; a minute in, the page's notice says the rows may be behind.
  session_sync_reconnecting: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: { width: 1200, height: 900 },
    sessionReplay: "reconnecting",
    outageAge: 10_000,
    readySelectors: ['[aria-label="Reconnecting…"]', '[data-connection="degraded"]', '[data-thread-anchor="34"]'],
    captureViewport: true,
  },
  session_sync_reconnecting_phone: {
    element: "#app",
    route: SESSION_ROUTE,
    viewport: PHONE,
    sessionReplay: "reconnecting",
    outageAge: 90_000,
    readySelectors: ['[aria-label="Reconnecting…"]', "::-p-text(may be out of date)", '[data-thread-anchor="34"]'],
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
    readySelectors: ['[data-thread-anchor="16"]'],
    captureViewport: true,
  },
};
