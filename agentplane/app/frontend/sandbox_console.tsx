/** The serial console of a VM Sandbox, read-only: the output from the moment this view attaches. */
import { Code, ScrollArea, Stack, Text } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { followStream } from "./live_stream";

// Terminal control sequences (CSI and OSC) are dropped: this is a text view, not a terminal.
// eslint-disable-next-line no-control-regex
const CONTROL_SEQUENCES = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\r/g;
const MAX_CHARS = 200_000;

export function consoleText(previous: string, output: string): string {
  return (previous + output.replace(CONTROL_SEQUENCES, "")).slice(-MAX_CHARS);
}

export function SandboxConsole({ sandbox }: { sandbox: string }): JSX.Element {
  const [text, setText] = useState("");
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setText("");
    setUnavailable(null);
    // One decoder per attach: a multi-byte character can straddle two events.
    const decoder = new TextDecoder();
    const stop = followStream(`/sandboxes/${encodeURIComponent(sandbox)}/console/stream`, {
      events: {
        output: (message) => {
          const bytes = Uint8Array.from(atob(message.data.slice(1, -1)), (char) => char.charCodeAt(0));
          setText((previous) => consoleText(previous, decoder.decode(bytes, { stream: true })));
        },
        unavailable: (message) => {
          setUnavailable(JSON.parse(message.data) as string);
          stop();
        },
      },
      onConnection: () => undefined,
    });
    return stop;
  }, [sandbox]);
  useEffect(() => bottom.current?.scrollIntoView?.({ block: "end" }), [text]);
  return (
    <Stack gap="xs">
      <Text size="sm" c="dimmed">
        Read-only serial console of the VM, from now on: earlier output is not replayed, and only one client can attach
        to a VM at a time.
      </Text>
      {unavailable !== null && (
        <Text c="red" role="alert">
          No console to attach: {unavailable}
        </Text>
      )}
      <ScrollArea h="60vh">
        <Code block aria-label="Console output">
          {text}
        </Code>
        <div ref={bottom} />
      </ScrollArea>
    </Stack>
  );
}
