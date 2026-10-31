/** Thread view recording controls in the app's persistent navigation. */
import { type JSX } from "react";
import { HistoryRecorderTools } from "../../../../util/frontend/transcript/history_recorder_tools";
import { threadViewRecorder } from "./thread_view_recorder";

export function ThreadViewRecorderTools(): JSX.Element {
  return (
    <HistoryRecorderTools
      recorder={threadViewRecorder}
      label="Thread view recorder"
      downloadPrefix="agentplane-thread-view-recording"
      position="top-start"
    />
  );
}
