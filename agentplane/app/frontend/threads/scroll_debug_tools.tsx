/** On-device scroll diagnostics available from the app's persistent navigation. */
import { ActionIcon, Menu } from "@mantine/core";
import { IconBug } from "../icons";
import { type JSX, useState } from "react";

import { downloadScrollDiagnostics, scrollCapture } from "./scroll_diagnostics";
import { markThreadViewEvent } from "./thread_view_timing";

export function ScrollDebugTools(): JSX.Element {
  const [recording, setRecording] = useState(() => scrollCapture.isRecording());
  return (
    <Menu position="top-start" withinPortal withArrow shadow="md">
      <Menu.Target>
        <ActionIcon
          size="sm"
          variant={recording ? "filled" : "subtle"}
          color={recording ? "red" : "gray"}
          aria-label={recording ? "Debug tools (recording)" : "Debug tools"}
          title="Scroll debug tools"
        >
          <IconBug size={16} />
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Scroll diagnostics (saved on this device only)</Menu.Label>
        {recording ? (
          <Menu.Item
            onClick={() => {
              const capture = scrollCapture.stopRecording();
              setRecording(false);
              if (capture) downloadScrollDiagnostics(capture);
            }}
          >
            Stop and download recording
          </Menu.Item>
        ) : (
          <Menu.Item
            onClick={() => {
              scrollCapture.startRecording();
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
