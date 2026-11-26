import { Button, Group, Paper, Text } from "@mantine/core";
import RFB from "@novnc/novnc";
import { useEffect, useRef, useState, type ReactElement } from "react";

interface Props {
  attached: boolean;
  opened: boolean;
  pending: boolean;
  onDetach: () => void;
  onReconnect: () => void;
}

export function Desktop({ attached, opened, pending, onDetach, onReconnect }: Props): ReactElement {
  const target = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("Desktop disconnected");

  useEffect(() => {
    if (!attached || !target.current) return;
    const url = new URL("/owner/viewer", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    let connection: RFB;
    try {
      connection = new RFB(target.current, url.href);
    } catch {
      setStatus("Desktop connection failed. Try again in a moment.");
      onDetach();
      return;
    }
    connection.scaleViewport = true;
    setStatus("Connecting to desktop…");
    const connected = () => setStatus("Desktop connected");
    const disconnected = (event: CustomEvent<{ clean: boolean }>) => {
      setStatus(event.detail.clean ? "Desktop disconnected" : "Desktop connection ended");
      onDetach();
    };
    connection.addEventListener("connect", connected);
    connection.addEventListener("disconnect", disconnected);
    return () => {
      connection.removeEventListener("connect", connected);
      connection.removeEventListener("disconnect", disconnected);
      connection.disconnect();
    };
  }, [attached, onDetach]);

  return (
    <Paper component="section" aria-label="Remote browser" withBorder radius="md" className="viewer">
      <Group justify="space-between" className="viewer-header">
        <Text size="sm" role="status">
          {status}
        </Text>
        {attached ? (
          <Button
            size="xs"
            variant="default"
            onClick={() => {
              setStatus("Desktop disconnected");
              onDetach();
            }}
          >
            Disconnect
          </Button>
        ) : (
          opened && (
            <Button size="xs" variant="default" disabled={pending} onClick={onReconnect}>
              Reconnect desktop
            </Button>
          )
        )}
      </Group>
      <div className="screen">
        <div id="display" aria-label="Remote display" ref={target} hidden={!attached} />
        {!attached && (
          <Text size="sm" c="dimmed">
            {opened ? "Reconnect to resume the remote browser." : "Open a provider sign-in to use the remote browser."}
          </Text>
        )}
      </div>
    </Paper>
  );
}
