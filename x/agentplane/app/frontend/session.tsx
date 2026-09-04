import {
  Accordion,
  ActionIcon,
  Badge,
  Button,
  Code,
  Group,
  Paper,
  ScrollArea,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import IconPlayerStop from "@tabler/icons-react/dist/esm/icons/IconPlayerStop.mjs";
import IconPower from "@tabler/icons-react/dist/esm/icons/IconPower.mjs";
import { Fragment, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router";

import { fromJson, type JsonValue } from "@bufbuild/protobuf";

import {
  displayableError,
  eventsUrl,
  findThread,
  interruptSession,
  renameThread,
  sendInput,
  shutdownSession,
  type ThreadView,
} from "./client";
import "./session.css";

import { EMPTY, reduce, timeline, type InputState, type Item, type Row, type SessionState, type Turn } from "./events";
import { FrameView } from "./frame";
import { Markdown } from "./markdown";
import { EventSchema, ItemKind, TurnStatus } from "./protocol_pb";

const KIND_LABELS: Partial<Record<ItemKind, string>> = {
  [ItemKind.ASSISTANT_TEXT]: "assistant",
  [ItemKind.TOOL_CALL]: "tool",
};

function InputView({ input }: { input: InputState }): JSX.Element {
  return (
    <Paper withBorder p="sm" bg="var(--mantine-color-default-hover)">
      <Group gap="xs">
        <Badge variant="light" color="grape">
          user
        </Badge>
        {input.state === "submitted" && <Badge color="yellow">sending</Badge>}
      </Group>
      {/* An input logged before the runner carried its text shows as its id. */}
      <Text style={{ whiteSpace: "pre-wrap" }}>{input.text || `input ${input.id}`}</Text>
    </Paper>
  );
}

/** The reasoning blocks showing their text, comma-separated by item id. */
const REASONING_PARAM = "reasoning";

/**
 * Reasoning stays folded, so an answer is not buried under the thinking that led to it, and each
 * block is opened on its own: which ones are open is recorded in the URL, by item id, so a reading
 * can be linked to and survives a reload.
 */
function ReasoningView({ item }: { item: Item }): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const open = new Set((searchParams.get(REASONING_PARAM) ?? "").split(",").filter((id) => id));
  function toggle(): void {
    if (!open.delete(item.id)) open.add(item.id);
    const next = new URLSearchParams(searchParams);
    if (open.size === 0) next.delete(REASONING_PARAM);
    else next.set(REASONING_PARAM, [...open].join(","));
    setSearchParams(next, { replace: true });
  }
  return (
    <Accordion variant="contained" chevronPosition="left" value={open.has(item.id) ? item.id : null} onChange={toggle}>
      <Accordion.Item value={item.id}>
        <Accordion.Control>
          <Group gap="xs">
            <Badge variant="light">reasoning</Badge>
            {!item.completed && <Badge color="yellow">streaming</Badge>}
          </Group>
        </Accordion.Control>
        <Accordion.Panel>
          <Text style={{ whiteSpace: "pre-wrap" }}>{item.text}</Text>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}

function ItemView({ item }: { item: Item }): JSX.Element {
  if (item.kind === ItemKind.REASONING) return <ReasoningView item={item} />;
  const label = KIND_LABELS[item.kind] ?? ItemKind[item.kind];
  return (
    <Paper withBorder p="sm">
      <Group gap="xs">
        <Badge variant="light">{label}</Badge>
        {item.toolName && <Text fw={600}>{item.toolName}</Text>}
        {!item.completed && <Badge color="yellow">streaming</Badge>}
        {item.succeeded === false && <Badge color="red">failed</Badge>}
      </Group>
      {item.text &&
        (item.kind === ItemKind.ASSISTANT_TEXT ? (
          <Markdown source={item.text} />
        ) : (
          <Text style={{ whiteSpace: "pre-wrap" }}>{item.text}</Text>
        ))}
      {item.argumentsJson && <Code block>{item.argumentsJson}</Code>}
      {item.output && <Code block>{item.output}</Code>}
    </Paper>
  );
}

function TurnHeader({ turn }: { turn: Turn }): JSX.Element {
  return (
    <Group gap="xs">
      <Text size="sm" c="dimmed">
        turn {turn.id}
      </Text>
      {turn.status !== null && (
        <Badge color={turn.status === TurnStatus.COMPLETED ? "green" : "orange"}>{TurnStatus[turn.status]}</Badge>
      )}
      {turn.error && <Text c="red">{turn.error}</Text>}
    </Group>
  );
}

function RowView({ row }: { row: Row }): JSX.Element {
  switch (row.kind) {
    case "turn":
      return <TurnHeader turn={row.turn} />;
    case "input":
      return <InputView input={row.input} />;
    case "item":
      return <ItemView item={row.item} />;
  }
}

/**
 * The session's title, edited where it is read: the field is the title, styled as one, and a hover
 * is the only hint that it takes typing. Enter commits and Escape puts the stored name back; moving
 * away commits too, so a rename is never lost by clicking elsewhere -- renaming again is one edit,
 * where losing what was typed is not recoverable at all.
 *
 * The placeholder is the session id, which is what an unnamed thread is called; a blank name clears
 * it back to that.
 */
function ThreadTitle({
  sessionId,
  thread,
  onRenamed,
  onError,
}: {
  sessionId: string;
  thread: ThreadView | null;
  onRenamed: (thread: ThreadView) => void;
  onError: (message: string) => void;
}): JSX.Element {
  const [draft, setDraft] = useState<string | null>(null);
  // The stored name while nothing is being typed, so a rename that arrives from elsewhere shows.
  const shown = draft ?? thread?.name ?? "";

  async function commit(): Promise<void> {
    if (draft === null || thread === null) return;
    const name = draft.trim() || null;
    setDraft(null);
    if (name === (thread.name ?? null)) return;
    try {
      onRenamed(await renameThread(thread.id, name));
    } catch (reason: unknown) {
      onError(displayableError(reason));
    }
  }

  return (
    // The title and the session id beside it; on a phone the pair takes a row of its own.
    <Group gap="xs" className="agentplane-thread-name">
      <TextInput
        aria-label="Thread name"
        // The thread exists once the bridge has opened the session; nothing to name before that.
        disabled={thread === null}
        variant="unstyled"
        size="xl"
        value={shown}
        placeholder={sessionId}
        maxLength={200}
        classNames={{ input: "agentplane-thread-name-input" }}
        style={{ flex: 1 }}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onBlur={() => void commit()}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") setDraft(null);
        }}
      />
      {thread?.name && (
        <Text size="sm" c="dimmed">
          {sessionId}
        </Text>
      )}
    </Group>
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
  const [status, setStatus] = useState("connecting");
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [thread, setThread] = useState<ThreadView | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);
  // The switch is in the URL, like the sandbox page's tab and the reasoning blocks that are open,
  // so a reading can be linked to and survives a reload.
  const [searchParams, setSearchParams] = useSearchParams();
  const showRaw = searchParams.get("raw") === "1";

  /** Sets a switch's parameter, leaving every other one where it is. */
  function setFlag(name: string, value: string, on: boolean): void {
    const next = new URLSearchParams(searchParams);
    if (on) next.set(name, value);
    else next.delete(name);
    setSearchParams(next, { replace: true });
  }

  useEffect(() => {
    // EventSource reconnects on its own and resends the last id it saw, which the bridge turns
    // into the runner's cursor, so a dropped connection loses nothing.
    const source = new EventSource(eventsUrl(sandbox, sessionId));
    source.addEventListener("attached", () => {
      setStatus("attached");
      // The bridge stores the thread before it sends `attached`, so it is there to look up now.
      findThread(sandbox, sessionId).then(setThread, (reason: unknown) => setError(displayableError(reason)));
    });
    source.addEventListener("event", (message: MessageEvent<string>) => {
      const event = fromJson(EventSchema, JSON.parse(message.data) as JsonValue);
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

  function composerKey(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (!(event.ctrlKey || event.metaKey)) {
      void submit();
      return;
    }
    // Insert the newline by hand: a textarea ignores Ctrl+Enter, and setting a controlled value
    // leaves the caret at the end, so put it back where the newline went.
    const field = event.currentTarget;
    const at = field.selectionStart;
    setDraft(`${draft.slice(0, at)}\n${draft.slice(field.selectionEnd)}`);
    requestAnimationFrame(() => field.setSelectionRange(at + 1, at + 1));
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
    // The session fills the window and the composer sits at its foot, rather than the page growing
    // past the fold and the composer going with it. `dvh` so a phone's collapsing URL bar does not
    // leave the composer under it; the subtraction is App's own `py="md"` on the Container, top and
    // bottom (app.tsx).
    <Stack h="calc(100dvh - 2 * var(--mantine-spacing-md))">
      <Group>
        <Button variant="subtle" onClick={onBack}>
          ← {sandbox}
        </Button>
        <ThreadTitle sessionId={sessionId} thread={thread} onRenamed={setThread} onError={setError} />
        <Badge>{status}</Badge>
        {state.harness && <Badge color={state.harness === "running" ? "green" : "gray"}>harness {state.harness}</Badge>}
        <ActionIcon
          variant="light"
          color="red"
          aria-label="Shut down harness"
          onClick={() => void run(() => shutdownSession(sandbox, sessionId))}
          disabled={state.harness !== "running"}
        >
          <IconPower size={16} />
        </ActionIcon>
        <Switch label="Raw frames" checked={showRaw} onChange={(e) => setFlag("raw", "1", e.currentTarget.checked)} />
      </Group>
      {error && <Text c="red">{error}</Text>}
      {/* `minHeight: 0` so this shrinks instead of pushing the composer off: a flex child
          defaults to its content's height as its floor. */}
      <ScrollArea style={{ flex: 1, minHeight: 0 }}>
        <Stack>
          {/* Raw: the whole session in sequence order, so what happened between two items — a
              stderr line, the harness starting — reads where it happened. Otherwise the turns,
              which group what the raw order interleaves. */}
          {showRaw ? (
            timeline(state).map(({ event, row }) => (
              <Fragment key={String(event.sequence)}>
                {row && <RowView row={row} />}
                <FrameView event={event} />
              </Fragment>
            ))
          ) : (
            <>
              {state.turns.map((turn) => (
                <Stack key={turn.id} gap="xs">
                  <TurnHeader turn={turn} />
                  {state.inputs
                    .filter((input) => input.turnId === turn.id)
                    .map((input) => (
                      <InputView key={input.id} input={input} />
                    ))}
                  {turn.itemIds.map((id) => state.items[id] && <ItemView key={id} item={state.items[id]} />)}
                </Stack>
              ))}
              {state.inputs
                .filter((input) => input.state === "submitted")
                .map((input) => (
                  <InputView key={input.id} input={input} />
                ))}
              {state.inputs
                .filter((input) => input.state === "rejected" || input.state === "uncertain")
                .map((input) => (
                  <Text key={input.id} c="orange">
                    input {input.id} {input.state} {input.detail}
                  </Text>
                ))}
            </>
          )}
          <div ref={bottom} />
        </Stack>
      </ScrollArea>
      <Group align="flex-end" gap="xs" wrap="nowrap">
        <Textarea
          style={{ flex: 1 }}
          placeholder="Enter sends, Ctrl+Enter for a new line"
          value={draft}
          autosize
          minRows={2}
          maxRows={12}
          disabled={state.harness !== "running"}
          onChange={(e) => setDraft(e.currentTarget.value)}
          onKeyDown={composerKey}
        />
        <ActionIcon
          size="lg"
          variant="light"
          aria-label="Interrupt"
          onClick={() => void run(() => interruptSession(sandbox, sessionId))}
          disabled={!activeTurn}
        >
          <IconPlayerStop size={16} />
        </ActionIcon>
      </Group>
    </Stack>
  );
}
