const SYNC_READY = ["#overview-heading", "#pairing-heading"];
const VIEWER_READY = [
  "#session-viewer-title",
  '[data-fold-kind="tool-run"][data-tool-count="5"]',
  '[data-tool-name="Bash"]',
  '[data-tool-name="Grep"]',
  '[data-tool-name="Glob"]',
  '[data-tool-name="Read"]',
  '[data-tool-name="Task"]',
  "[data-tool-output-image]",
];
const TOOL_RESULT_READY = ["#session-viewer-title", '[data-tool-name="Read"]', "[data-tool-output-image]"];
const SUBAGENT_READY = [
  "#session-viewer-title",
  '[data-subagent-activity][data-subagent-tool-count="2"]',
  '[data-subagent-latest-tool="Grep"]',
  '[data-parent-tool-use-id="agent-17"] [data-tool-name="Grep"]',
];

export const SCENARIOS = {
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
  SessionSubagent: { element: "#app", readySelectors: SUBAGENT_READY },
  SessionSubagent_mobile: {
    element: "#app",
    readySelectors: SUBAGENT_READY,
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
