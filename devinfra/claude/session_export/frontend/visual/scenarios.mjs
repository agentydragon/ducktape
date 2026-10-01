const READY = ["#session-viewer-title", "article"];

export const SCENARIOS = {
  SessionViewer: { element: "#app", readySelectors: READY },
  SessionViewer_dark: { element: "#app", readySelectors: READY, colorScheme: "dark" },
  SessionViewer_mobile: {
    element: "#app",
    readySelectors: READY,
    viewport: { width: 420, height: 900 },
  },
};
