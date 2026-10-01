const SYNC_READY = ["#overview-heading", "#pairing-heading"];
const VIEWER_READY = ["#session-viewer-title", '[data-fold-kind="tool"]', '[data-fold-kind="activity"]'];

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
