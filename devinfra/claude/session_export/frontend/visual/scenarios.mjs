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

export const SCENARIOS = {
  SessionViewer: { element: "#app", readySelectors: VIEWER_READY },
  SessionViewer_dark: { element: "#app", readySelectors: VIEWER_READY, colorScheme: "dark" },
  SessionViewer_mobile: {
    element: "#app",
    readySelectors: VIEWER_READY,
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
