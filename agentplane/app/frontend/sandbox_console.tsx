/** The serial console of a VM Sandbox, read-only: the output from the moment this view attaches. */
import { Code, ScrollArea, Stack, Text } from "@mantine/core";
import { type JSX, useEffect, useRef, useState } from "react";

import { consoleText } from "./console_text";
import { followStream } from "./live_stream";

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
  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "end" });
  }, [text]);
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
