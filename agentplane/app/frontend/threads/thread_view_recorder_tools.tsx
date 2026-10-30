/** Thread view recording controls in the app's persistent navigation. */
import { ActionIcon, Menu } from "@mantine/core";
import { IconBug } from "../icons";
import { type JSX, useState } from "react";

import { markThreadViewEvent, downloadThreadViewRecording, threadViewRecorder } from "./thread_view_recorder";

export function ThreadViewRecorderTools(): JSX.Element {
  const [recording, setRecording] = useState(() => threadViewRecorder.isRecording());
  return (
    <Menu position="top-start" withinPortal withArrow shadow="md">
      <Menu.Target>
        <ActionIcon
          size="sm"
          variant={recording ? "filled" : "subtle"}
          color={recording ? "red" : "gray"}
          aria-label={recording ? "Thread view recorder (recording)" : "Thread view recorder"}
          title="Thread view recorder"
        >
          <IconBug size={16} />
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Thread view recorder (saved on this device only)</Menu.Label>
        {recording ? (
          <Menu.Item
            onClick={() => {
              const capture = threadViewRecorder.stopRecording();
              setRecording(false);
              if (capture) downloadThreadViewRecording(capture);
            }}
          >
            Stop and download recording
          </Menu.Item>
        ) : (
          <Menu.Item
            onClick={() => {
              threadViewRecorder.startRecording();
              setRecording(true);
            }}
          >
            Start recording
          </Menu.Item>
        )}
        {recording && <Menu.Item onClick={() => markThreadViewEvent({ kind: "marker" })}>Mark a jump</Menu.Item>}
        <Menu.Label>Review row keys and metadata before sharing.</Menu.Label>
      </Menu.Dropdown>
    </Menu>
  );
}
