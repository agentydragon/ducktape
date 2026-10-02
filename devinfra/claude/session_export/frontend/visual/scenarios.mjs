const SYNC_READY = ["#overview-heading", "#pairing-heading"];
const VIEWER_READY = [
  '[aria-label="Session history"]',
  '[data-fold-kind="tool-run"][data-tool-count="5"]',
  "[data-tool-run-toggle]",
];
const TOOL_RESULT_READY = ['[aria-label="Session history"]', '[data-tool-name="Read"]', "[data-tool-output-image]"];
const READ_FILE_READY = [
  '[aria-label="Session history"]',
  '[data-tool-file-path="src/session-viewer.ts"]',
  "[data-tool-file-preview]",
];
const EVENT_VISIBILITY_READY = [
  '[aria-label="Session history"]',
  '[data-fold-kind="message"][data-message-role="user"]',
  '[data-fold-kind="message"][data-message-role="assistant"]',
];
const SUBAGENT_READY = [
  '[aria-label="Session history"]',
  '[data-subagent-activity][data-subagent-tool-count="2"]',
  '[data-subagent-latest-tool="Grep"]',
];
const PEER_HOLD_READY = [
  '[aria-label="Session history"]',
  '[data-fold-kind="peer-message"][data-peer-from="plan-agent"]',
  '[data-fold-kind="peer-hold"][data-peer-state="held"]',
  '[data-fold-kind="peer-hold"][data-peer-state="dropped"]',
];
const PEER_MESSAGE_READY = [
  '[aria-label="Session history"]',
  '[data-fold-kind="peer-message"][data-peer-from="review-agent"][data-peer-handback="true"]',
];

const LOCAL_COMMAND_READY = [
  '[aria-label="Session history"]',
  '[data-fold-kind="context"][data-context-model="claude-sonnet-4-5"]',
  '[data-fold-kind="stats"][data-stats-state="data"]',
  '[data-fold-kind="usage"]',
  '[data-fold-kind="status"]',
];

const NOISY_READY = ['#app[data-noisy-ready="true"]'];
const SIDEBAR_EXPANDED_READY = ['#app[data-noisy-ready="true"][data-sidebar-ready="expanded"]'];
const LATEST_TAIL_READY = ['#app[data-latest-tail-ready="true"]'];
const HISTORY_ANCHOR_READY = ['#app[data-history-anchor-ready="true"]'];

export const SCENARIOS = {
  SessionNoisyActivity: { element: "#app", readySelectors: ['#app[data-activity-ready="true"]'] },
  SessionNoisyActivityExpanded: { element: "#app", readySelectors: ['#app[data-activity-ready="true"]'] },
  SessionNoisyActivityExpanded_mobile: {
    element: "#app",
    readySelectors: ['#app[data-activity-ready="true"]'],
    viewport: { width: 420, height: 900 },
  },
  SessionNoisySidebar: { element: "#app", readySelectors: SIDEBAR_EXPANDED_READY },
  SessionNoisySidebarCollapsed: {
    element: "#app",
    readySelectors: ['#app[data-noisy-ready="true"][data-sidebar-ready="collapsed"]'],
  },
  SessionNoisySidebarWide: {
    element: "#app",
    readySelectors: ['#app[data-noisy-ready="true"][data-sidebar-ready="wide"]'],
  },
  SessionNoisySidebar_mobile: {
    element: "#app",
    readySelectors: ['#app[data-noisy-ready="true"][data-sidebar-ready="mobile-open"]'],
    viewport: { width: 420, height: 900 },
  },
  SessionNoisy: { element: "#app", readySelectors: NOISY_READY },
  SessionNoisy_mobile: { element: "#app", readySelectors: NOISY_READY, viewport: { width: 420, height: 900 } },
  SessionNoisyThinking: { element: "#app", readySelectors: NOISY_READY },
  SessionNoisyRaw: { element: "#app", readySelectors: NOISY_READY },
  SessionNoisyHook: { element: "#app", readySelectors: NOISY_READY },
  SessionNoisyHook_mobile: { element: "#app", readySelectors: NOISY_READY, viewport: { width: 420, height: 900 } },
  SessionLatestFirstTail: { element: "#app", readySelectors: LATEST_TAIL_READY },
  SessionLatestFirstAnchor: { element: "#app", readySelectors: HISTORY_ANCHOR_READY },
  SessionViewer: { element: "#app", readySelectors: VIEWER_READY },
  SessionViewer_dark: { element: "#app", readySelectors: VIEWER_READY, colorScheme: "dark" },
  SessionViewer_mobile: {
    element: "#app",
    readySelectors: VIEWER_READY,
    viewport: { width: 420, height: 900 },
  },
  SessionToolResult: { element: "#app", readySelectors: TOOL_RESULT_READY },
  SessionToolResult_mobile: {
    element: "#app",
    readySelectors: TOOL_RESULT_READY,
    viewport: { width: 420, height: 900 },
  },
  SessionReadFileResult: { element: "#app", readySelectors: READ_FILE_READY },
  SessionReadFileResult_mobile: {
    element: "#app",
    readySelectors: READ_FILE_READY,
    viewport: { width: 420, height: 900 },
  },
  SessionEventVisibility: { element: "#app", readySelectors: EVENT_VISIBILITY_READY },
  SessionEventVisibility_mobile: {
    element: "#app",
    readySelectors: EVENT_VISIBILITY_READY,
    viewport: { width: 420, height: 900 },
  },
  SessionSubagent: { element: "#app", readySelectors: SUBAGENT_READY },
  SessionSubagent_mobile: {
    element: "#app",
    readySelectors: SUBAGENT_READY,
    viewport: { width: 420, height: 900 },
  },
  SessionPeerHold: { element: "#app", readySelectors: PEER_HOLD_READY },
  SessionPeerHold_mobile: {
    element: "#app",
    readySelectors: PEER_HOLD_READY,
    viewport: { width: 420, height: 900 },
  },
  SessionPeerMessage: { element: "#app", readySelectors: PEER_MESSAGE_READY },
  SessionPeerMessage_mobile: {
    element: "#app",
    readySelectors: PEER_MESSAGE_READY,
    viewport: { width: 420, height: 900 },
  },
  SessionLocalCommandRows: { element: "#app", readySelectors: LOCAL_COMMAND_READY },
  SessionLocalCommandRows_mobile: {
    element: "#app",
    readySelectors: LOCAL_COMMAND_READY,
    viewport: { width: 420, height: 900 },
  },
  SessionSync: { element: "#app", readySelectors: SYNC_READY },
  SessionSync_paired_dark: { element: "#app", readySelectors: SYNC_READY, colorScheme: "dark" },
  SessionSync_paired_mobile: {
    element: "#app",
    readySelectors: SYNC_READY,
    viewport: { width: 420, height: 900 },
  },
};
