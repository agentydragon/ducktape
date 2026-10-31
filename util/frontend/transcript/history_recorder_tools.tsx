import { ActionIcon, Menu, type MenuProps } from "@mantine/core";
import IconBug from "@tabler/icons-react/dist/esm/icons/IconBug.mjs";
import { type JSX, useState } from "react";

import { downloadHistoryRecording, type HistoryRecorder } from "./history_recorder";

/** Compact controls for an application-owned transcript recorder. */
export function HistoryRecorderTools({
  recorder,
  label,
  downloadPrefix,
  position,
}: {
  recorder: HistoryRecorder;
  label: string;
  downloadPrefix: string;
  position?: MenuProps["position"];
}): JSX.Element {
  const [recording, setRecording] = useState(() => recorder.isRecording());
  return (
    <Menu position={position} withinPortal withArrow shadow="md">
      <Menu.Target>
        <ActionIcon
          size="sm"
          variant={recording ? "filled" : "subtle"}
          color={recording ? "red" : "gray"}
          aria-label={recording ? `${label} (recording)` : label}
          title={label}
        >
          <IconBug size={16} />
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{label} (saved on this device only)</Menu.Label>
        {recording ? (
          <Menu.Item
            onClick={() => {
              const capture = recorder.stopRecording();
              setRecording(false);
              if (capture) downloadHistoryRecording(capture, downloadPrefix);
            }}
          >
            Stop and download recording
          </Menu.Item>
        ) : (
          <Menu.Item
            onClick={() => {
              recorder.startRecording();
              setRecording(true);
            }}
          >
            Start recording
          </Menu.Item>
        )}
        {recording && <Menu.Item onClick={() => recorder.mark({ kind: "marker" })}>Mark a jump</Menu.Item>}
        <Menu.Label>Review row keys and metadata before sharing.</Menu.Label>
      </Menu.Dropdown>
    </Menu>
  );
}
