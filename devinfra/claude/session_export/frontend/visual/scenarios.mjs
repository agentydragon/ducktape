const READY = ["#overview-heading", "#pairing-heading", "#session-viewer-title", "article"];

export const SCENARIOS = {
  SessionSync: { element: "#app", readySelectors: READY },
  SessionSync_paired_dark: { element: "#app", readySelectors: READY, colorScheme: "dark" },
  SessionSync_paired_mobile: {
    element: "#app",
    readySelectors: READY,
    viewport: { width: 420, height: 900 },
  },
};
