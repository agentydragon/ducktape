import { HistoryRecorder } from "../../../../util/frontend/transcript/history_recorder";

declare global {
  interface Window {
    /** Set by the Playwright init script before the app loads, not by production code. */
    __agentplaneThreadViewMarksEnabled?: boolean;
  }
}

/** One recorder across the page: it survives switching between Threads. */
export const threadViewRecorder: HistoryRecorder = new HistoryRecorder({
  markPrefix: "agentplane:thread-view:",
  testsEnabled: () => window.__agentplaneThreadViewMarksEnabled === true,
});
