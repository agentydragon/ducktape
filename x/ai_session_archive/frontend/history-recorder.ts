import { HistoryRecorder } from "../../../util/frontend/transcript/history_recorder";

/** Survives session and captured-version navigation within the page. */
export const historyRecorder: HistoryRecorder = new HistoryRecorder({ markPrefix: "ai-session-archive:history:" });
