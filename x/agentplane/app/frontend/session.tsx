import { Badge, Button, Code, Group, Paper, ScrollArea, Stack, Switch, Text, Textarea, Title } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { displayableError, eventsUrl, interruptSession, sendInput, shutdownSession, type Event } from "./client";
import { EMPTY, reduce, type Item, type SessionState } from "./events";

const KIND_LABELS: Record<string, string> = {
  ITEM_KIND_ASSISTANT_TEXT: "assistant",
  ITEM_KIND_REASONING: "reasoning",
  ITEM_KIND_TOOL_CALL: "tool",
};

function ItemView({ item }: { item: Item }): JSX.Element {
  const label = KIND_LABELS[item.kind] ?? item.kind;
  return (
    <Paper withBorder p="sm">
      <Group gap="xs">
        <Badge variant="light">{label}</Badge>
        {item.toolName && <Text fw={600}>{item.toolName}</Text>}
        {!item.completed && <Badge color="yellow">streaming</Badge>}
        {item.succeeded === false && <Badge color="red">failed</Badge>}
      </Group>
      {item.text && <Text style={{ whiteSpace: "pre-wrap" }}>{item.text}</Text>}
      {item.argumentsJson && <Code block>{item.argumentsJson}</Code>}
      {item.output && <Code block>{item.output}</Code>}
    </Paper>
  );
}

export function SessionView({
  sandbox,
  sessionId,
  onBack,
}: {
  sandbox: string;
  sessionId: string;
  onBack: () => void;
}): JSX.Element {
  const [state, setState] = useState<SessionState>(EMPTY);
  const [raw, setRaw] = useState<Event[]>([]);
  const [showRaw, setShowRaw] = useState(false);
  const [status, setStatus] = useState("connecting");
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const bottom = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // EventSource reconnects on its own and resends the last id it saw, which the bridge turns
    // into the runner's cursor, so a dropped connection loses nothing.
    const source = new EventSource(eventsUrl(sandbox, sessionId));
    source.addEventListener("attached", () => setStatus("attached"));
    source.addEventListener("event", (message: MessageEvent<string>) => {
      const event = JSON.parse(message.data) as Event;
      setRaw((events) => [...events, event]);
      setState((current) => reduce(current, event));
    });
    // The runner ending the stream is final: a reconnect would Open the session again, which
    // restarts a shut-down harness. Only a dropped connection is left to EventSource's own retry.
    source.addEventListener("end", () => {
      source.close();
      setStatus("stream ended");
    });
    source.addEventListener("error", (message: globalThis.Event) => {
      if ("data" in message) {
        source.close();
        setStatus(`runner: ${String((message as MessageEvent<string>).data)}`);
      } else {
        setStatus("reconnecting");
      }
    });
    return () => source.close();
  }, [sandbox, sessionId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [state.lastSequence]);

  async function submit(): Promise<void> {
    const text = draft.trim();
    if (!text) return;
    try {
      await sendInput(sandbox, sessionId, crypto.randomUUID(), text);
      setDraft("");
      setError(null);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  }

  async function run(action: () => Promise<void>): Promise<void> {
    try {
      await action();
      setError(null);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  }

  const activeTurn = state.turns.find((turn) => turn.status === null);
  return (
    <Stack>
      <Group>
        <Button variant="subtle" onClick={onBack}>
          ← {sandbox}
        </Button>
        <Title order={3}>{sessionId}</Title>
        <Badge>{status}</Badge>
        {state.harness && <Badge color={state.harness === "running" ? "green" : "gray"}>harness {state.harness}</Badge>}
        <Switch label="Raw frames" checked={showRaw} onChange={(e) => setShowRaw(e.currentTarget.checked)} />
      </Group>
      {error && <Text c="red">{error}</Text>}
      <ScrollArea h="60vh">
        <Stack>
          {showRaw
            ? raw.map((event) => (
                <Code key={event.sequence} block>
                  {event.native
                    ? `${event.sequence} ${event.native.direction} ${event.native.line}`
                    : `${event.sequence} ${JSON.stringify(event)}`}
                </Code>
              ))
            : state.turns.map((turn) => (
                <Stack key={turn.id} gap="xs">
                  <Group gap="xs">
                    <Text size="sm" c="dimmed">
                      turn {turn.id}
                    </Text>
                    {turn.status && (
                      <Badge color={turn.status === "TURN_STATUS_COMPLETED" ? "green" : "orange"}>{turn.status}</Badge>
                    )}
                    {turn.error && <Text c="red">{turn.error}</Text>}
                  </Group>
                  {turn.itemIds.map((id) => state.items[id] && <ItemView key={id} item={state.items[id]} />)}
                </Stack>
              ))}
          {state.inputs
            .filter((input) => input.state === "rejected" || input.state === "uncertain")
            .map((input) => (
              <Text key={input.id} c="orange">
                input {input.id} {input.state} {input.detail}
              </Text>
            ))}
          <div ref={bottom} />
        </Stack>
      </ScrollArea>
      <Textarea
        placeholder="Send to the agent"
        value={draft}
        autosize
        minRows={2}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit();
        }}
      />
      <Group>
        <Button onClick={() => void submit()} disabled={!draft.trim() || state.harness !== "running"}>
          Send
        </Button>
        <Button
          variant="light"
          onClick={() => void run(() => interruptSession(sandbox, sessionId))}
          disabled={!activeTurn}
        >
          Interrupt
        </Button>
        <Button
          variant="light"
          color="red"
          onClick={() => void run(() => shutdownSession(sandbox, sessionId))}
          disabled={state.harness !== "running"}
        >
          Shut down harness
        </Button>
      </Group>
    </Stack>
  );
}
