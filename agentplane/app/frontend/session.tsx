import {
  Accordion,
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Menu,
  Paper,
  Select,
  ScrollArea,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from "@mantine/core";
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import IconPlayerStop from "@tabler/icons-react/dist/esm/icons/IconPlayerStop.mjs";
import IconPower from "@tabler/icons-react/dist/esm/icons/IconPower.mjs";
import {
  type JSX,
  Fragment,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type KeyboardEvent,
} from "react";
import { useSearchParams } from "react-router";

import { create, toJsonString } from "@bufbuild/protobuf";

import { displayableError, eventsUrl, getThread, renameThread, models, type ThreadView } from "./client";
import "./session.css";

import {
  timelineBlocks,
  type ConversationContent,
  type InputState,
  type Item,
  type ItemGroup,
  type SessionState,
  type Turn,
} from "./events";
import { FrameView } from "./frame";
import { appliedModel, catchingUp, EventStream, type Connection } from "./event_stream";
import { HighlightedText } from "./json_view";
import { Markdown } from "./markdown";
import { ItemKind, TurnStatus } from "../../protocol/event_pb";
import { CommandSchema } from "../../protocol/command_pb";
import { AttachedSchema } from "../../runner/protocol_pb";
import { useCommandSubmission } from "./command_submission";
import { PendingCommands } from "./pending_commands";
import { ConversationScroll } from "./conversation_scroll";
import { LiveStatus, liveSandboxesUrl, useLive, type SandboxesSnapshot } from "./live";

const KIND_LABELS: Partial<Record<ItemKind, string>> = {
  [ItemKind.ASSISTANT_TEXT]: "assistant",
  [ItemKind.TOOL_CALL]: "tool",
};

/** A transient/binary state, shown as a small colored dot rather than a labeled badge: the label
 * is still there for a screen reader, and for anyone hovering or (on a touch/keyboard device)
 * focusing it, just not spelled out at rest. `breathing` pulses the dot, for a state that is
 * still ongoing (streaming) rather than settled (failed, sending); `style` lets a caller take it
 * out of flow instead of the default inline placement. */
function StatusDot({
  color,
  label,
  breathing,
  style,
}: {
  color: string;
  label: string;
  breathing?: boolean;
  style?: CSSProperties;
}): JSX.Element {
  return (
    <Tooltip label={label} events={{ hover: true, focus: true, touch: true }}>
      <Box
        component="span"
        role="img"
        aria-label={label}
        title={label}
        tabIndex={0}
        className={breathing ? "agentplane-status-dot agentplane-breathing-dot" : "agentplane-status-dot"}
        style={{ backgroundColor: `var(--mantine-color-${color}-6)`, ...style }}
      />
    </Tooltip>
  );
}

/** Role reads from position and color, not a label: the assistant's items are already full-width
 * (`ItemView`), so only user input needs a distinct treatment -- a right-aligned bubble. The
 * runner supplies this only after the harness confirms the native message, so it is always a
 * settled transcript entry, not a guessed delivery state. */
function InputView({ input }: { input: InputState }): JSX.Element {
  return (
    <Group justify="flex-end">
      <Paper className="agentplane-user-bubble" p="sm">
        {/* An input logged before the runner carried its text shows as its id. */}
        <Text style={{ whiteSpace: "pre-wrap" }}>{input.text || `input ${input.id}`}</Text>
      </Paper>
    </Group>
  );
}

/** The reasoning blocks showing their text, comma-separated by item id. */
const REASONING_PARAM = "reasoning";

/**
 * Reasoning stays folded, so an answer is not buried under the thinking that led to it, and each
 * block is opened on its own: which ones are open is recorded in the URL, by item id, so a reading
 * can be linked to and survives a reload.
 */
function IncompleteStatus({ live, style }: { live: boolean; style?: CSSProperties }): JSX.Element {
  return (
    <StatusDot
      breathing={live}
      color={live ? "yellow" : "gray"}
      label={live ? "Streaming" : "Incomplete in retained history"}
      style={style}
    />
  );
}

function ReasoningView({ item, live }: { item: Item; live: boolean }): JSX.Element {
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
    <Accordion
      variant="contained"
      chevronPosition="left"
      classNames={{ chevron: "agentplane-accordion-chevron" }}
      value={open.has(item.id) ? item.id : null}
      onChange={toggle}
    >
      <Accordion.Item value={item.id}>
        <Accordion.Control>
          <Group gap="xs">
            <Badge variant="light">reasoning</Badge>
            {!item.completed && <IncompleteStatus live={live} />}
          </Group>
        </Accordion.Control>
        <Accordion.Panel>
          <Markdown source={item.text} />
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}

function ItemView({ item, live }: { item: Item; live: boolean }): JSX.Element {
  if (item.kind === ItemKind.REASONING) return <ReasoningView item={item} live={live} />;
  // Assistant text needs no kind label: it's the only unlabeled content in the transcript besides
  // the user's own bubble, so the absence of a badge already reads as "the reply" -- a status dot,
  // when there is one, is all it still needs.
  const isAssistant = item.kind === ItemKind.ASSISTANT_TEXT;
  const label = KIND_LABELS[item.kind] ?? ItemKind[item.kind];
  const streaming = !item.completed;
  const failed = item.succeeded === false;
  return (
    <Paper withBorder p="sm" style={{ position: "relative" }}>
      {(!isAssistant || failed) && (
        <Group gap="xs">
          {!isAssistant && <Badge variant="light">{label}</Badge>}
          {item.toolName && <Text fw={600}>{item.toolName}</Text>}
          {/* Assistant text has no header row to toggle a dot inside of -- see the pinned dot
              below, which doesn't grow/shrink the card as text streams in. */}
          {!isAssistant && streaming && <IncompleteStatus live={live} />}
          {failed && <StatusDot color="red" label="Failed" />}
        </Group>
      )}
      {item.text &&
        (item.kind === ItemKind.ASSISTANT_TEXT ? (
          <Markdown source={item.text} />
        ) : (
          <Text style={{ whiteSpace: "pre-wrap" }}>{item.text}</Text>
        ))}
      {item.argumentsJson && <HighlightedText text={item.argumentsJson} />}
      {item.output && <HighlightedText text={item.output} />}
      {/* Pinned to the card, not the header: growing reply text must not make a badge row pop in
          and out above it, so this sits out of flow at the corner instead of a separate line. */}
      {isAssistant && streaming && (
        <IncompleteStatus live={live} style={{ position: "absolute", right: 8, bottom: 8 }} />
      )}
    </Paper>
  );
}

/** "12 tool calls, 3 reasoning steps": each kind counted separately, so a mixed run still reads
 * at a glance without claiming a false total. */
function summarizeRun(items: Item[]): string {
  const toolCalls = items.filter((item) => item.kind === ItemKind.TOOL_CALL).length;
  const reasoning = items.filter((item) => item.kind === ItemKind.REASONING).length;
  const parts: string[] = [];
  if (toolCalls > 0) parts.push(`${toolCalls} tool call${toolCalls === 1 ? "" : "s"}`);
  if (reasoning > 0) parts.push(`${reasoning} reasoning step${reasoning === 1 ? "" : "s"}`);
  return parts.join(", ");
}

/** A run of tool calls/reasoning collapsed behind a summary, so a long chain of intermediate
 * steps does not bury the answer that follows it; expanding shows each step as usual. */
function ItemRunView({ items, live }: { items: Item[]; live: boolean }): JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <Accordion
      variant="contained"
      chevronPosition="left"
      classNames={{ chevron: "agentplane-accordion-chevron" }}
      value={open ? "run" : null}
      onChange={() => setOpen(!open)}
    >
      <Accordion.Item value="run">
        <Accordion.Control>
          <Group gap="xs">
            <Badge variant="light">{summarizeRun(items)}</Badge>
            {items.some((item) => !item.completed) && <IncompleteStatus live={live} />}
            {items.some((item) => item.succeeded === false) && <StatusDot color="red" label="Failed" />}
          </Group>
        </Accordion.Control>
        <Accordion.Panel>
          <Stack gap="xs">
            {items.map((item) => (
              <ItemView key={item.id} item={item} live={live} />
            ))}
          </Stack>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}

function ItemGroupView({ group, live }: { group: ItemGroup; live: boolean }): JSX.Element {
  if (group.kind === "single") return <ItemView item={group.item} live={live} />;
  // Reasoning already has its own URL-addressable disclosure; keep that state as the only
  // disclosure for a lone reasoning item while tool calls use the run summary consistently.
  if (group.items.length === 1 && group.items[0].kind === ItemKind.REASONING) {
    return <ItemView item={group.items[0]} live={live} />;
  }
  return <ItemRunView items={group.items} live={live} />;
}

function TurnHeader({ turn }: { turn: Turn }): JSX.Element {
  return (
    <Group gap="xs">
      <Text size="sm" c="dimmed">
        turn {turn.id}
      </Text>
      {turn.status !== null && (
        <Badge
          color={turn.status === TurnStatus.COMPLETED ? "green" : turn.status === TurnStatus.FAILED ? "red" : "orange"}
        >
          {TurnStatus[turn.status]}
        </Badge>
      )}
    </Group>
  );
}

function ContentView({ content, live }: { content: ConversationContent; live: boolean }): JSX.Element {
  switch (content.kind) {
    case "turn":
      return <TurnHeader turn={content.turn} />;
    case "input":
      return <InputView input={content.input} />;
    case "items":
      return <ItemGroupView group={content.group} live={live} />;
    case "control": {
      const observation = content.entry.event?.observation;
      if (observation?.case === "turnCompleted" && observation.value.status === TurnStatus.FAILED) {
        return (
          <Alert color="red" title="Turn failed" role="alert">
            <Text size="sm" c="dimmed">
              turn {observation.value.turnId}
            </Text>
            <Text style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {observation.value.error || "The harness reported no error details."}
            </Text>
          </Alert>
        );
      }
      return (
        <Text size="sm" c="dimmed">
          {observation?.case === "modelChanged" && `Model changed to ${observation.value.model}`}
          {observation?.case === "turnCompleted" &&
            `Turn ${observation.value.turnId}: ${TurnStatus[observation.value.status]}`}
        </Text>
      );
    }
  }
}

function contentIdentity(content: ConversationContent): string {
  switch (content.kind) {
    case "turn":
      return `turn ${content.turn.id} · first event ${content.turn.firstCursor}`;
    case "input":
      return `harness message ${content.input.id} · first event ${content.input.firstCursor}`;
    case "items": {
      const items = content.group.kind === "single" ? [content.group.item] : content.group.items;
      return items.map((item) => `item ${item.id} · first event ${item.firstCursor}`).join("; ");
    }
    case "control":
      return `observed at event ${content.entry.cursor}`;
  }
}

/**
 * The session's title, edited where it is read: the field is the title, styled as one, and a hover
 * is the only hint that it takes typing. Enter commits and Escape puts the stored name back; moving
 * away commits too, so a rename is never lost by clicking elsewhere -- renaming again is one edit,
 * where losing what was typed is not recoverable at all.
 *
 * The placeholder is the Thread id; a blank name clears
 * it back to that.
 */
function ThreadTitle({
  threadId,
  thread,
  onRenamed,
  onError,
}: {
  threadId: string;
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
    // The title and its stable id; on a phone the pair takes a row of its own.
    <Group gap="xs" className="agentplane-thread-name">
      <TextInput
        aria-label="Thread name"
        disabled={thread === null}
        variant="unstyled"
        size="xl"
        value={shown}
        placeholder={threadId}
        maxLength={200}
        classNames={{ input: "agentplane-thread-name-input" }}
        style={{ flex: "1 1 12rem", minWidth: 0 }}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onBlur={() => void commit()}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") setDraft(null);
        }}
      />
      {thread?.name && (
        <Text size="sm" c="dimmed" style={{ overflowWrap: "anywhere", maxWidth: "100%" }}>
          {threadId}
        </Text>
      )}
    </Group>
  );
}

/** Session attachment and the harness process (`state.harness`) are two independent
 * state machines; this collapses them into one dot by severity, worst axis first, so the header
 * doesn't need a badge per axis. */
function connectionStatus(
  connection: Connection,
  replaying: boolean,
  harness: SessionState["harness"]
): { color: string; breathing?: boolean; label: string } {
  if (connection.kind === "failed") return { color: "red", label: "Event stream stopped" };
  if (connection.kind === "connecting") return { color: "yellow", breathing: true, label: "Connecting…" };
  if (connection.kind === "reconnecting") return { color: "yellow", breathing: true, label: "Reconnecting…" };
  if (replaying) return { color: "yellow", breathing: true, label: "Catching up…" };
  if (harness === "lost") return { color: "red", label: "Harness lost" };
  if (connection.kind === "following" && harness === null) {
    return { color: "yellow", label: "Following Thread log · no harness observed" };
  }
  if (connection.kind === "ended") return { color: "gray", label: `Stream ended · harness ${harness ?? "unknown"}` };
  const status = "Following Thread log · last observed";
  if (harness === "stopped") return { color: "gray", label: `${status} · harness stopped` };
  return { color: "green", label: `${status} · harness ${harness ?? "unknown"}` };
}

interface SessionViewProps {
  threadId: string;
  onBack: () => void;
}

export function SessionView(props: SessionViewProps): JSX.Element {
  // All local state belongs to this target, including drafts and outstanding HTTP continuations.
  return <SessionContents key={props.threadId} {...props} />;
}

function SessionContents({ threadId, onBack }: SessionViewProps): JSX.Element {
  const [stream] = useState(() => new EventStream(eventsUrl(threadId)));
  const snapshot = useSyncExternalStore(stream.subscribe, stream.getSnapshot);
  const { conversation: state, attached, connection } = snapshot;
  const replaying = catchingUp(snapshot);
  const model = appliedModel(snapshot);
  const environment = useLive<SandboxesSnapshot>(liveSandboxesUrl());
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const submitting = useRef(false);
  const [thread, setThread] = useState<ThreadView | null>(null);
  const commands = useCommandSubmission(threadId, stream);
  const sandbox = environment.snapshot?.sandboxes.find((candidate) => candidate.name === thread?.sandbox);
  const inventoryFresh = environment.connection === "connected" && environment.health?.fresh;
  const sandboxAvailable = inventoryFresh && sandbox?.state === "running";
  const unavailable = !sandboxAvailable || replaying || connection.kind === "failed" || connection.kind === "ended";
  const receiving = !unavailable && connection.kind === "following" && state.harness === "running";
  const [modelOptions, setModelOptions] = useState<string[]>([]);
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
    let active = true;
    const onError = (reason: unknown): void => {
      if (active) setError(displayableError(reason));
    };
    getThread(threadId).then((value) => {
      if (active) setThread(value);
    }, onError);
    return () => {
      active = false;
    };
  }, [threadId]);

  useEffect(() => {
    if (!thread) return;
    let active = true;
    models().then(
      (catalog) => {
        if (active) setModelOptions(catalog[thread.harness] ?? []);
      },
      (reason: unknown) => {
        if (active) setError(displayableError(reason));
      }
    );
    return () => {
      active = false;
    };
  }, [thread?.harness]);

  function selectModel(next: string | null): void {
    if (!thread || !next || next === model) return;
    commands.submit(
      create(CommandSchema, {
        commandId: crypto.randomUUID(),
        operation: { case: "changeModel", value: { model: next } },
      })
    );
  }

  // Guard repeated Enter within one render, not the lifetime of any HTTP request or command.
  useEffect(() => {
    submitting.current = false;
  }, [draft]);

  function submit(): void {
    if (!thread || !draft.trim() || submitting.current || unavailable || state.harness !== "running") return;
    submitting.current = true;
    if (
      commands.submit(
        create(CommandSchema, {
          commandId: crypto.randomUUID(),
          operation: { case: "submitInput", value: { text: draft } },
        })
      )
    ) {
      setDraft("");
    } else {
      submitting.current = false;
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

  const activeTurn = state.turns.find((turn) => turn.status === null);
  return (
    // App owns the viewport height; use only the space left below its navigation.
    <Stack style={{ flex: 1, minHeight: 0 }}>
      <Group style={{ flexShrink: 0 }}>
        <Button variant="subtle" onClick={onBack}>
          ← Threads
        </Button>
        <ThreadTitle threadId={threadId} thread={thread} onRenamed={setThread} onError={setError} />
      </Group>
      <LiveStatus live={environment} />
      {thread && environment.snapshot && !sandbox && (
        <Text role="status" c="dimmed">
          {inventoryFresh
            ? "Sandbox no longer exists. Showing archived Thread history."
            : "Sandbox absent from last inventory snapshot. Current availability unknown."}
        </Text>
      )}
      {sandbox && sandbox.state !== "running" && (
        <Text role="status" c="dimmed">
          Last observed Sandbox state: {sandbox.state}. Showing retained Thread history.
        </Text>
      )}
      {error && <Text c="red">{error}</Text>}
      {commands.error && (
        <Text c="red" role="alert">
          Local command recovery: {commands.error}
        </Text>
      )}
      {connection.kind === "failed" && (
        <Text c="red" role="alert">
          Event stream stopped: {connection.reason}. Showing verified history through event {state.lastCursor}.
        </Text>
      )}
      {showRaw && attached && (
        <details>
          <summary>
            Operational runner snapshot · advertised event {String(attached.lastCursor)} · consumed event{" "}
            {state.lastCursor}
          </summary>
          <Text size="xs" c="dimmed">
            Not a replayed Event or the conversation projection. This snapshot can be ahead of retained history.
          </Text>
          <ScrollArea.Autosize mah={160}>
            <HighlightedText text={toJsonString(AttachedSchema, attached)} />
          </ScrollArea.Autosize>
        </details>
      )}
      <ConversationScroll>
        {showRaw && (
          <Text size="xs" c="dimmed">
            Cards are current aggregates through event {state.lastCursor}, anchored where first observed. Evidence below
            each card preserves Event order; it is not a snapshot of the card at that earlier time.
          </Text>
        )}
        {timelineBlocks(state).map(({ content, entries }) => {
          const group = content?.kind === "items" ? content.group : null;
          const item = group?.kind === "single" ? group.item : group?.items[0];
          const live = receiving && item !== undefined && activeTurn?.itemIds.includes(item.id) === true;
          return (
            <Fragment key={String(entries[0].cursor)}>
              {content && (
                <Stack gap="xs" data-conversation-anchor={String(entries[0].cursor)}>
                  {showRaw && (
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      {contentIdentity(content)}
                    </Text>
                  )}
                  <ContentView content={content} live={live} />
                </Stack>
              )}
              {showRaw && entries.map((entry) => <FrameView key={String(entry.cursor)} entry={entry} />)}
            </Fragment>
          );
        })}
      </ConversationScroll>
      {/* The model picker and stop control sit under the composer, not the header: on a phone
          that's the row already in thumb reach, and it's one thing keeping the header a
          two-line-tall row instead of three. */}
      <Stack gap="xs" style={{ flexShrink: 0 }}>
        <PendingCommands
          commands={commands}
          retryDisabled={unavailable || !thread || state.harness !== "running"}
          raw={showRaw}
        />
        {replaying && connection.kind !== "failed" && (
          <Text size="sm" c="dimmed" role="status">
            Catching up: {state.lastCursor} / {String(attached?.lastCursor)} events
          </Text>
        )}
        <Textarea
          placeholder="Enter sends, Ctrl+Enter for a new line"
          value={draft}
          autosize
          minRows={2}
          maxRows={12}
          disabled={unavailable || !thread || state.harness !== "running"}
          onChange={(e) => setDraft(e.currentTarget.value)}
          onKeyDown={composerKey}
        />
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <StatusDot {...connectionStatus(connection, replaying, state.harness)} />
            <Select
              aria-label="Model"
              data={modelOptions}
              value={model}
              onChange={(next) => void selectModel(next)}
              placeholder={connection.kind === "failed" ? "Model unavailable" : replaying ? "Catching up…" : "Model"}
              disabled={unavailable || !thread || state.harness !== "running"}
              w={200}
            />
          </Group>
          <Group gap="xs" wrap="nowrap">
            {/* Opens upward: the composer sits at the bottom of the viewport, so there's rarely
                room below the trigger -- Mantine's own Floating-UI flip would land here anyway,
                but "top-end" states the intent rather than leaving it to the fallback. */}
            <Menu position="top-end" withArrow closeOnItemClick={false} shadow="md">
              <Menu.Target>
                <ActionIcon variant="light" aria-label="More">
                  <IconDotsVertical size={16} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item
                  rightSection={
                    <Switch checked={showRaw} readOnly tabIndex={-1} size="xs" style={{ pointerEvents: "none" }} />
                  }
                  onClick={() => setFlag("raw", "1", !showRaw)}
                >
                  Raw frames
                </Menu.Item>
                <Menu.Divider />
                <Menu.Item
                  color="red"
                  leftSection={<IconPower size={15} />}
                  disabled={unavailable || !thread || state.harness !== "running"}
                  closeMenuOnClick
                  onClick={() => {
                    if (!thread) return;
                    commands.submit(
                      create(CommandSchema, {
                        commandId: crypto.randomUUID(),
                        operation: { case: "stopRunnerSession", value: {} },
                      })
                    );
                  }}
                >
                  Shut down harness
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
            <ActionIcon
              size="lg"
              variant="light"
              color="red"
              aria-label="Interrupt"
              onClick={() => {
                if (!thread || !activeTurn) return;
                commands.submit(
                  create(CommandSchema, {
                    commandId: crypto.randomUUID(),
                    operation: { case: "interruptTurn", value: { turnId: activeTurn.id } },
                  })
                );
              }}
              disabled={unavailable || !thread || !activeTurn}
            >
              <IconPlayerStop size={16} />
            </ActionIcon>
          </Group>
        </Group>
      </Stack>
    </Stack>
  );
}
