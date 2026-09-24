import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Menu,
  Paper,
  Select,
  Stack,
  Text,
  Textarea,
  Tooltip,
} from "@mantine/core";
import { create, fromJson, type JsonValue } from "@bufbuild/protobuf";
import { useVirtualizer } from "@tanstack/react-virtual";
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import IconPlayerStop from "@tabler/icons-react/dist/esm/icons/IconPlayerStop.mjs";
import IconPower from "@tabler/icons-react/dist/esm/icons/IconPower.mjs";
import IconSend from "@tabler/icons-react/dist/esm/icons/IconSend.mjs";
import IconZoomCode from "@tabler/icons-react/dist/esm/icons/IconZoomCode.mjs";
import {
  type CSSProperties,
  type JSX,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventSchema, ItemKind, TurnStatus } from "../../protocol/event_pb";
import {
  command,
  threadEvidence,
  threadNativeFrames,
  displayableError,
  getThread,
  models,
  type EvidencePage,
  type NativeFramePage,
  type ThreadView,
} from "./client";
import {
  decimalBigInt,
  useThreadSync,
  type PayloadRef,
  type ThreadEntity,
  type ThreadState,
  type ThreadWindow,
} from "./thread_sync";
import { LocalCommands, type LocalCommand, type LocalCommandSnapshot } from "./local_commands";
import { liveSandboxesUrl, useLive, type SandboxesSnapshot } from "./live";
import { HighlightedText, JsonView } from "./json_view";
import { Markdown } from "./markdown";
import { RetainedDisclosure, RetainedDisclosureProvider, useRetainedDisclosure } from "./retained_disclosures";
import { ChronologicalDebugLink, ChronologicalDebugProvider } from "./chronological_debug";
import { ThreadTitle } from "./thread_title";
import "./projected_session.css";

const EMPTY_LOCAL: LocalCommandSnapshot = { commands: [], error: null };

export function pruneCommandErrors(errors: Map<string, string>, commandIds: ReadonlySet<string>): Map<string, string> {
  if (Array.from(errors.keys()).every((id) => commandIds.has(id))) return errors;
  return new Map(Array.from(errors).filter(([id]) => commandIds.has(id)));
}

// An interrupt is usually the operator's own doing, so an interrupted turn reads as ordinary.
const TURN_OUTCOMES: Record<TurnStatus, { label: string; prominent: boolean }> = {
  [TurnStatus.UNSPECIFIED]: { label: "Turn ended without a status", prominent: true },
  [TurnStatus.COMPLETED]: { label: "Turn completed", prominent: false },
  [TurnStatus.INTERRUPTED]: { label: "Turn interrupted", prominent: false },
  [TurnStatus.FAILED]: { label: "Turn failed", prominent: true },
  [TurnStatus.PROCESS_LOST]: { label: "Turn lost", prominent: true },
};

interface LifecyclePresentation {
  label: string;
  /** A prominent row is an alert; any other reads as dimmed text. */
  prominent: boolean;
  diagnostic: string | null;
}

function lifecyclePresentation(observation: string, event: unknown): LifecyclePresentation {
  const parsed = fromJson(EventSchema, event as JsonValue).observation;
  switch (parsed.case) {
    case "turnStarted":
      return { label: "Turn started", prominent: false, diagnostic: null };
    case "turnCompleted": {
      const { status, error } = parsed.value;
      const diagnostic = error || (status === TurnStatus.FAILED ? "The harness reported no error details." : null);
      return { ...TURN_OUTCOMES[status], diagnostic };
    }
    case "modelChanged":
      return { label: `Model changed to ${parsed.value.model}`, prominent: false, diagnostic: null };
    case "harnessStarted":
      return { label: "Harness started", prominent: false, diagnostic: null };
    case "harnessExited":
      return {
        label: parsed.value.exitCode ? `Harness exited with code ${parsed.value.exitCode}` : "Harness exited",
        prominent: false,
        diagnostic: null,
      };
    case "harnessLost":
      return { label: "Harness lost", prominent: true, diagnostic: null };
    default:
      return { label: observation.replaceAll("_", " "), prominent: false, diagnostic: null };
  }
}

/** How a body renders: the agent's prose (assistant text, reasoning) as Markdown, tool arguments and
 * output as code (highlighted when it is JSON), and the operator's own input verbatim, as typed. */
type BodyFormat = "markdown" | "code" | "text";

function Body({ reference, format }: { reference: PayloadRef | null; format: BodyFormat }): JSX.Element {
  if (!reference) return <Text c="dimmed">Body not observed</Text>;
  return <PayloadText reference={reference} format={format} />;
}

function PayloadText({ reference, format }: { reference: PayloadRef; format: BodyFormat }): JSX.Element {
  const { body, error, retry } = useThreadSync().usePayload(reference);
  return (
    <>
      {error && (
        <p role="alert">
          Payload synchronization stopped: {error} <button onClick={retry}>Retry payload synchronization</button>
        </p>
      )}
      {body === null ? (
        <Text c="dimmed">Loading complete revision…</Text>
      ) : (
        <FormattedBody body={body} format={format} />
      )}
    </>
  );
}

function FormattedBody({ body, format }: { body: string; format: BodyFormat }): JSX.Element {
  switch (format) {
    case "markdown":
      return <Markdown source={body} />;
    case "code":
      return <HighlightedText text={body} />;
    case "text":
      return <VerbatimText text={body} />;
  }
}

function VerbatimText({ text }: { text: string }): JSX.Element {
  return (
    <Text className="agentplane-verbatim" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
      {text}
    </Text>
  );
}

function LazyBody({ label, ...body }: { label: string; reference: PayloadRef; format: BodyFormat }): JSX.Element {
  const id = `${body.reference.projection_epoch}:${body.reference.owner_id}:${body.reference.field}`;
  return (
    <RetainedDisclosure id={id} summary={label}>
      <Body {...body} />
    </RetainedDisclosure>
  );
}

function EvidenceFramesPage({
  threadId,
  entity,
  observationCursor,
}: {
  threadId: string;
  entity: ThreadEntity;
  observationCursor: string;
}): JSX.Element {
  const [page, setPage] = useState<NativeFramePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [afterSequence, setAfterSequence] = useState("0");
  const request = useRef<AbortController | null>(null);
  const scope = {
    projectionEpoch: entity.projectionEpoch,
    entityKind: entity.entityKind,
    entityId: entity.entityId,
  };
  useEffect(() => {
    load();
    return () => request.current?.abort();
  }, []);
  const load = (after = "0"): void => {
    if (loading) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    void threadNativeFrames(threadId, scope, observationCursor, after, controller.signal)
      .then(
        (value) => {
          if (!controller.signal.aborted) {
            setPage(value);
            setAfterSequence(after);
          }
        },
        (reason: unknown) => {
          if (!controller.signal.aborted) setError(displayableError(reason));
        }
      )
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  };
  return (
    <Stack gap="xs">
      {error && <Text c="red">{error}</Text>}
      {page?.frames.map((frame) =>
        frame.availability === "present" ? (
          <JsonView key={frame.source_sequence} value={frame.entry} />
        ) : (
          <Text size="xs" key={frame.source_sequence}>
            Raw frame {frame.source_sequence} unavailable
          </Text>
        )
      )}
      {!page && error && (
        <Button loading={loading} onClick={() => load()}>
          Retry raw frames
        </Button>
      )}
      {afterSequence !== "0" && (
        <Button variant="subtle" disabled={loading} onClick={() => load()}>
          First frames
        </Button>
      )}
      {page?.next_after_sequence && (
        <Button variant="subtle" loading={loading} onClick={() => load(page.next_after_sequence ?? "0")}>
          Load more frames
        </Button>
      )}
    </Stack>
  );
}

function EvidenceFrames(props: { threadId: string; entity: ThreadEntity; observationCursor: string }): JSX.Element {
  const { entity } = props;
  const id = `${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:frames:${props.observationCursor}`;
  return (
    <RetainedDisclosure id={id} summary={`Observation ${props.observationCursor} raw frames`}>
      <EvidenceFramesPage key={id} {...props} />
    </RetainedDisclosure>
  );
}

function EvidencePageView({ threadId, entity }: { threadId: string; entity: ThreadEntity }): JSX.Element {
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [afterCursor, setAfterCursor] = useState("0");
  const request = useRef<AbortController | null>(null);
  const scope = {
    projectionEpoch: entity.projectionEpoch,
    entityKind: entity.entityKind,
    entityId: entity.entityId,
  };
  useEffect(() => {
    load();
    return () => request.current?.abort();
  }, []);
  const load = (after = "0"): void => {
    if (loading) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    void threadEvidence(threadId, scope, after, controller.signal)
      .then(
        (value) => {
          if (!controller.signal.aborted) {
            setPage(value);
            setAfterCursor(after);
          }
        },
        (reason: unknown) => {
          if (!controller.signal.aborted) setError(displayableError(reason));
        }
      )
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  };
  return (
    <Stack gap="xs">
      {error && <Text c="red">{error}</Text>}
      {page?.observations.map((observation) => (
        <Stack gap="xs" key={observation.observation_cursor} data-evidence-observation={observation.observation_cursor}>
          {observation.has_native ? (
            <EvidenceFrames
              key={observation.observation_cursor}
              threadId={threadId}
              entity={entity}
              observationCursor={observation.observation_cursor}
            />
          ) : (
            <Text size="xs" key={observation.observation_cursor}>
              Observation {observation.observation_cursor} has no native frame
            </Text>
          )}
          <ChronologicalDebugLink observationCursor={observation.observation_cursor} />
        </Stack>
      ))}
      {page?.next_after_cursor && (
        <Button variant="subtle" loading={loading} onClick={() => load(page.next_after_cursor ?? "0")}>
          Load more evidence
        </Button>
      )}
      {afterCursor !== "0" && (
        <Button variant="subtle" disabled={loading} onClick={() => load()}>
          First evidence
        </Button>
      )}
    </Stack>
  );
}

function evidenceDisclosure(entity: ThreadEntity): string {
  return `${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:evidence`;
}

/** An icon, not a disclosure row, so it adds no height to its card: it sits in the card's header
 * row where there is one, and is pinned out of flow at a corner (`style`) where there is not.
 *
 * Labelled by a native `title`, not a Mantine `Tooltip`: rows move under a resting pointer while the
 * thread streams, and a Tooltip opening then positions itself with `flushSync` from a ResizeObserver
 * callback, committing the whole list's pending re-render mid-delivery (a "ResizeObserver loop"). */
function EvidenceToggle({ entity, style }: { entity: ThreadEntity; style?: CSSProperties }): JSX.Element {
  const [open, setOpen] = useRetainedDisclosure(evidenceDisclosure(entity));
  return (
    <ActionIcon
      size="xs"
      variant={open ? "light" : "subtle"}
      color="gray"
      aria-label="Evidence"
      title="Evidence"
      aria-expanded={open}
      onClick={() => setOpen(!open)}
      style={style}
    >
      <IconZoomCode size={14} />
    </ActionIcon>
  );
}

function EvidencePanel({ threadId, entity }: { threadId: string; entity: ThreadEntity }): JSX.Element {
  const id = evidenceDisclosure(entity);
  const [open] = useRetainedDisclosure(id);
  return open ? <EvidencePageView key={id} threadId={threadId} entity={entity} /> : <></>;
}

export function EntityCard({
  threadId,
  entity,
  live,
}: {
  threadId: string;
  entity: ThreadEntity;
  live: boolean;
}): JSX.Element {
  if (entity.entityKind === "confirmed_input") {
    return (
      <Group justify="flex-end" align="flex-start" gap="xs" wrap="nowrap" data-thread-anchor={entity.cursor.toString()}>
        {/* The bubble has no header row: beside its top corner, in the width it leaves free, the
            icon neither grows the bubble nor covers its text. */}
        <EvidenceToggle entity={entity} />
        <Paper className="agentplane-user-bubble" p="sm">
          <Body reference={entity.inputRef} format="text" />
          <EvidencePanel threadId={threadId} entity={entity} />
        </Paper>
      </Group>
    );
  }
  if (entity.entityKind === "lifecycle" && "observation" in entity.state) {
    const { label, prominent, diagnostic } = lifecyclePresentation(entity.state.observation, entity.state.event);
    const wrapped = { whiteSpace: "pre-wrap", overflowWrap: "anywhere" } as const;
    if (!prominent) {
      return (
        <Stack gap={0} style={{ position: "relative" }} data-thread-anchor={entity.cursor.toString()}>
          <Text size="xs" c="dimmed">
            {label}
          </Text>
          <EvidenceToggle entity={entity} style={{ position: "absolute", top: 0, right: 0 }} />
          {diagnostic && (
            <Text size="xs" c="dimmed" style={wrapped}>
              {diagnostic}
            </Text>
          )}
          <EvidencePanel threadId={threadId} entity={entity} />
        </Stack>
      );
    }
    return (
      <Alert
        color="red"
        title={label}
        role="alert"
        style={{ position: "relative" }}
        data-thread-anchor={entity.cursor.toString()}
      >
        <EvidenceToggle entity={entity} style={{ position: "absolute", top: 8, right: 8 }} />
        {diagnostic && (
          <Text size="sm" style={wrapped}>
            {diagnostic}
          </Text>
        )}
        <EvidencePanel threadId={threadId} entity={entity} />
      </Alert>
    );
  }
  if (entity.entityKind === "command" || entity.entityKind === "view_state") return <></>;
  if (!("kind" in entity.state)) return <></>;
  const kind = entity.state.kind;
  const tool = entity.state.tool_name;
  const completion = entity.state.completion;
  const reasoning = kind === ItemKind.REASONING;
  const streaming = live && completion === null;
  return (
    <Paper p="sm" withBorder data-thread-anchor={entity.cursor.toString()}>
      <Group justify="space-between" mb="xs">
        <Badge variant="light">{kind === ItemKind.TOOL_CALL ? tool || "tool" : "assistant"}</Badge>
        <Group gap="xs">
          {streaming && (
            <Badge role="img" aria-label="Streaming">
              Streaming
            </Badge>
          )}
          {completion === null && !streaming && (
            <Badge role="img" aria-label="Incomplete">
              Incomplete
            </Badge>
          )}
          <EvidenceToggle entity={entity} />
        </Group>
      </Group>
      {entity.textRef &&
        (reasoning ? (
          <LazyBody label="Reasoning" reference={entity.textRef} format="markdown" />
        ) : (
          <Body reference={entity.textRef} format="markdown" />
        ))}
      {entity.argumentsRef && <LazyBody label="Arguments" reference={entity.argumentsRef} format="code" />}
      {entity.outputRef && <LazyBody label="Output" reference={entity.outputRef} format="code" />}
      <EvidencePanel threadId={threadId} entity={entity} />
    </Paper>
  );
}

function useProjectedCommands(threadId: string, entities: ThreadEntity[]) {
  const store = useMemo(() => new LocalCommands(threadId), [threadId]);
  const local = useSyncExternalStore(store.subscribe, store.getSnapshot, () => EMPTY_LOCAL);
  const [errors, setErrors] = useState(new Map<string, string>());
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const active = useRef(new Set<string>());
  useEffect(() => {
    setErrors((previous) =>
      pruneCommandErrors(previous, new Set(local.commands.map((command) => command.command.commandId)))
    );
  }, [local.commands]);
  const effectedCommandIds = useMemo(
    () =>
      new Set([...entities.flatMap((row) => ("origin_command_ids" in row.state ? row.state.origin_command_ids : []))]),
    [entities]
  );
  useEffect(() => store.observeCommandIds(effectedCommandIds), [effectedCommandIds, store]);

  const deliver = useCallback(
    async (value: LocalCommand): Promise<void> => {
      const id = value.command.commandId;
      if (active.current.has(id)) return;
      active.current.add(id);
      try {
        store.acknowledge(value.command, await command(threadId, value.command));
        setErrors((previous) => {
          if (!previous.has(id)) return previous;
          const next = new Map(previous);
          next.delete(id);
          return next;
        });
      } catch (reason) {
        if (store.getSnapshot().commands.some((command) => command.command.commandId === id))
          setErrors((previous) => new Map(previous).set(id, displayableError(reason)));
      } finally {
        active.current.delete(id);
      }
    },
    [store, threadId]
  );
  // Deliver each retained command not yet seen admitted once on mount, and again when the browser
  // comes back online after a failed attempt. An exact retry is safe: the server answers it from its
  // archive once admitted, and the runner deduplicates by command id.
  useEffect(() => {
    for (const value of store.getSnapshot().commands) if (value.admission === null) void deliver(value);
  }, [deliver, store]);
  useEffect(() => {
    const redeliverFailed = () => {
      for (const value of store.getSnapshot().commands)
        if (value.admission === null && errors.has(value.command.commandId)) void deliver(value);
    };
    window.addEventListener("online", redeliverFailed);
    return () => window.removeEventListener("online", redeliverFailed);
  }, [deliver, errors, store]);
  function submit(value: Command): boolean {
    try {
      void deliver(store.remember(value));
      setSubmissionError(null);
      return true;
    } catch (reason) {
      setSubmissionError(displayableError(reason));
      return false;
    }
  }
  return { local, errors, submissionError, submit, deliver, store };
}

function SelectedCommandOutcomes({
  commands,
  store,
  errors,
  deliver,
}: {
  commands: LocalCommand[];
  store: LocalCommands;
  errors: ReadonlyMap<string, string>;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element {
  const rows = useThreadSync().useCommandRows(commands.slice(0, 128).map((value) => value.command.commandId));
  return <SelectedCommandRows rows={rows} commands={commands} store={store} errors={errors} deliver={deliver} />;
}

function SelectedCommandRows({
  rows,
  commands,
  store,
  errors,
  deliver,
}: {
  rows: ThreadEntity[];
  commands: LocalCommand[];
  store: LocalCommands;
  errors: ReadonlyMap<string, string>;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element {
  useEffect(() => {
    for (const row of rows) {
      if ("outcome" in row.state && row.state.outcome === "effected") store.dismiss(row.entityId);
    }
  }, [rows, store]);
  const byId = new Map(rows.map((row) => [row.entityId, row]));
  return (
    <Stack role="region" aria-label="Pending commands" gap="xs">
      {commands.map((value) => {
        const row = byId.get(value.command.commandId);
        const admitted = row !== undefined || value.admission !== null;
        const terminal = row && "outcome" in row.state && ["failed", "noop"].includes(row.state.outcome);
        return (
          <Paper key={value.command.commandId} data-command-id={value.command.commandId} p="xs" withBorder>
            {terminal && row && "outcome" in row.state ? (
              <>
                <Text c={row.state.outcome === "failed" ? "red" : undefined}>
                  {commandOutcomeLabel(row.state.operation, row.state.outcome)}
                  {row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}
                </Text>
                {row.inputRef && <Body reference={row.inputRef} format="text" />}
                <Button variant="subtle" onClick={() => store.dismiss(row.entityId)}>
                  Dismiss
                </Button>
              </>
            ) : (
              <>
                <Text size="sm">{admitted ? "Saved · awaiting effect" : "Saved locally · awaiting admission"}</Text>
                {value.command.operation.case === "submitInput" && (
                  <VerbatimText text={value.command.operation.value.text} />
                )}
                {value.command.operation.case === "changeModel" && (
                  <Text>Change model to {value.command.operation.value.model}</Text>
                )}
                {value.command.operation.case === "interruptTurn" && (
                  <Text>Interrupt turn {value.command.operation.value.turnId}</Text>
                )}
                {value.command.operation.case === "stopRunnerSession" && <Text>Shut down harness</Text>}
                {!admitted && errors.get(value.command.commandId) && (
                  <Text c="red">{errors.get(value.command.commandId)}</Text>
                )}
                {!admitted && <Button onClick={() => void deliver(value)}>Retry</Button>}
              </>
            )}
          </Paper>
        );
      })}
    </Stack>
  );
}

function commandOutcomeLabel(operation: string, outcome: string): string {
  const subject =
    {
      submit_input: "Input",
      change_model: "Model change",
      interrupt_turn: "Interrupt",
      stop_runner_session: "Harness shutdown",
    }[operation] ?? "Command";
  return `${subject} ${outcome === "failed" ? "failed" : outcome === "noop" ? "not applied" : "applied"}`;
}

// How close the top of the loaded rows comes to the viewport's before the page before them loads.
const LOAD_OLDER_WITHIN = 80;

function VirtualizedHistory({
  threadId,
  segments,
  running,
  activeTurn,
  history,
}: {
  threadId: string;
  segments: ThreadEntity[];
  running: boolean;
  activeTurn: string | null;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder">;
}): JSX.Element {
  const viewport = useRef<HTMLDivElement>(null);
  const contents = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const previousScrollTop = useRef(0);
  // Every bottom the viewport has had since the last scroll event or content resize was handled.
  // A return to the bottom lands on whichever one was current when it ran; a card can grow in
  // that task or an earlier one before the browser dispatches the scroll event.
  const recentBottoms = useRef<number[]>([]);
  const pointerScrolling = useRef(false);
  const captureNextScroll = useRef(false);
  const scrolledSinceInput = useRef(false);
  const touchY = useRef<number | null>(null);
  const restorationFrame = useRef<number | null>(null);
  const restoringAnchor = useRef<string | null>(null);
  const restorationSize = useRef<number | null>(null);
  const previousCount = useRef(segments.length);
  const previousFirstKey = useRef<string | null>(null);
  const readingAnchor = useRef<{ key: string; cursor: string; offset: number } | null>(null);
  const cancelRestoration = () => {
    if (restorationFrame.current !== null) cancelAnimationFrame(restorationFrame.current);
    restorationFrame.current = null;
    restorationSize.current = null;
    restoringAnchor.current = null;
  };
  const recordBottom = (element: HTMLDivElement) => {
    const bottom = element.scrollHeight - element.clientHeight;
    if (!recentBottoms.current.includes(bottom)) recentBottoms.current.push(bottom);
  };
  const followPreviousBottom = (element: HTMLDivElement) => {
    // A programmatic return to the old bottom can be delivered after a card grows. Preserve
    // it before restoring a stale reader anchor, while an explicit user gesture owns its scroll.
    if (captureNextScroll.current) return false;
    if (!recentBottoms.current.some((bottom) => Math.abs(element.scrollTop - bottom) <= 2)) return false;
    atBottom.current = true;
    cancelRestoration();
    element.scrollTop = element.scrollHeight;
    return true;
  };
  function correctRestoration(): number | null {
    const anchor = readingAnchor.current;
    const element = viewport.current;
    if (!anchor || !element || restoringAnchor.current !== anchor.key) return null;
    const row = element.querySelector<HTMLElement>(`[data-thread-anchor="${anchor.cursor}"]`);
    if (!row) return null;
    const correction = row.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset;
    element.scrollTop += correction;
    return correction;
  }
  const virtualizer = useVirtualizer({
    count: segments.length,
    getScrollElement: () => viewport.current,
    estimateSize: () => 180,
    getItemKey: (index) => `${segments[index]?.entityKind}:${segments[index]?.entityId}`,
    measureElement: (element) => element.getBoundingClientRect().height,
    overscan: 5,
    onChange: (instance, sync) => {
      // A card can resize before virtual-core applies its measured transform. Wait for
      // that measurement rather than guessing how many animation frames it requires.
      if (viewport.current && followPreviousBottom(viewport.current)) return;
      if (atBottom.current) {
        restorationSize.current = null;
        restoringAnchor.current = null;
        return;
      }
      if (sync || restorationSize.current === null || instance.getTotalSize() === restorationSize.current) return;
      restorationSize.current = null;
      if (restorationFrame.current !== null) cancelAnimationFrame(restorationFrame.current);
      restorationFrame.current = requestAnimationFrame(() => {
        restorationFrame.current = null;
        if (viewport.current && followPreviousBottom(viewport.current)) return;
        if (correctRestoration() !== null) {
          restoringAnchor.current = null;
        }
      });
    },
  });
  // ResizeObserver below preserves the first visible row explicitly. This is an
  // instance hook in the pinned virtual-core version, rather than an option.
  virtualizer.shouldAdjustScrollPositionOnItemSizeChange = () => false;
  const expectUserScroll = () => {
    if (captureNextScroll.current) return;
    captureNextScroll.current = true;
    scrolledSinceInput.current = false;
  };
  // A gesture asks for the page before the oldest row as its scroll events reach the top. This asks
  // where no scroll event will: rows too few to scroll, a gesture that ended at the top, a page that
  // landed with the reader still there. A gesture or restoration in progress has not settled where
  // the reader is, and until the tail shows there is no top to reach.
  const loadOlderAtTop = () => {
    const element = viewport.current;
    if (
      element &&
      segments.length > 0 &&
      history.olderAvailable &&
      !captureNextScroll.current &&
      restoringAnchor.current === null &&
      element.scrollTop < LOAD_OLDER_WITHIN
    )
      history.loadOlder();
  };
  const captureReadingAnchor = (element: HTMLDivElement) => {
    const viewportTop = element.getBoundingClientRect().top;
    const first = [...element.querySelectorAll<HTMLElement>("[data-thread-anchor]")].find(
      (candidate) => candidate.getBoundingClientRect().bottom > viewportTop
    );
    const firstEntity = first
      ? segments.find((entity) => entity.cursor.toString() === first.dataset.threadAnchor)
      : undefined;
    if (first && firstEntity) {
      readingAnchor.current = {
        key: `${firstEntity.entityKind}:${firstEntity.entityId}`,
        cursor: firstEntity.cursor.toString(),
        offset: first.getBoundingClientRect().top - viewportTop,
      };
    }
  };
  const restoreAnchor = (anchor: { key: string; cursor: string; offset: number }, awaitMeasurement = false) => {
    const index = segments.findIndex((entity) => `${entity.entityKind}:${entity.entityId}` === anchor.key);
    if (index < 0) return;
    cancelRestoration();
    restoringAnchor.current = anchor.key;
    const correctFromDom = (): number | null => {
      const element = viewport.current;
      const row = element?.querySelector<HTMLElement>(`[data-thread-anchor="${anchor.cursor}"]`);
      if (!element || !row) return null;
      const currentOffset = row.getBoundingClientRect().top - element.getBoundingClientRect().top;
      const correction = currentOffset - anchor.offset;
      element.scrollTop += correction;
      return correction;
    };
    const correction = correctFromDom();
    if (correction === null) virtualizer.scrollToIndex(index, { align: "start" });
    if (awaitMeasurement && (correction === null || Math.abs(correction) <= 2)) {
      restorationSize.current = virtualizer.getTotalSize();
      return;
    }
    restorationFrame.current = requestAnimationFrame(() => {
      if (restoringAnchor.current === anchor.key) correctFromDom();
      restorationFrame.current = requestAnimationFrame(() => {
        if (restoringAnchor.current === anchor.key) restoringAnchor.current = null;
        restorationFrame.current = null;
      });
    });
  };
  useLayoutEffect(() => {
    const element = viewport.current;
    const firstKey = segments[0] ? `${segments[0].entityKind}:${segments[0].entityId}` : null;
    if (element && atBottom.current && segments.length > previousCount.current)
      element.scrollTop = element.scrollHeight;
    if (
      element &&
      !atBottom.current &&
      segments.length > 0 &&
      readingAnchor.current &&
      (previousCount.current === 0 || previousFirstKey.current !== firstKey)
    ) {
      const index = segments.findIndex(
        (entity) => `${entity.entityKind}:${entity.entityId}` === readingAnchor.current?.key
      );
      if (index >= 0) restoreAnchor(readingAnchor.current);
    }
    previousCount.current = segments.length;
    previousFirstKey.current = firstKey;
  }, [segments, virtualizer]);
  useLayoutEffect(() => {
    const element = viewport.current;
    const content = contents.current;
    if (!element || !content) return;
    recordBottom(element);
    const observer = new ResizeObserver(() => {
      // A scrollbar drag or programmatic equivalent can reach the old bottom in the same task
      // that grows the last card, before the browser dispatches its scroll event. Preserve that
      // user choice across the resize without interpreting arbitrary layout movement as intent.
      if (followPreviousBottom(element) || atBottom.current) element.scrollTop = element.scrollHeight;
      // Content can resize while a wheel, touch, or key scroll is still settling. Its
      // measured rows do not describe the reader's final position yet; scrollend will
      // capture that position before a later resize restoration is eligible.
      else if (!captureNextScroll.current && readingAnchor.current) restoreAnchor(readingAnchor.current, true);
      recentBottoms.current = [element.scrollHeight - element.clientHeight];
    });
    observer.observe(content);
    // A body arriving for a remounted or streaming card is a DOM mutation, and the scroll event
    // of a return to the bottom that follows it in the same frame precedes the ResizeObserver
    // delivery for it. The mutation callback runs before any later task, so record its bottom.
    const mutations = new MutationObserver(() => recordBottom(element));
    mutations.observe(content, { subtree: true, childList: true, characterData: true, attributes: true });
    return () => {
      observer.disconnect();
      mutations.disconnect();
      cancelRestoration();
    };
  }, [segments, virtualizer]);
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const onScrollEnd = () => {
      if (restoringAnchor.current !== null || !captureNextScroll.current) return;
      captureReadingAnchor(element);
      captureNextScroll.current = false;
      loadOlderAtTop();
    };
    element.addEventListener("scrollend", onScrollEnd);
    return () => element.removeEventListener("scrollend", onScrollEnd);
  }, [segments]);
  useEffect(loadOlderAtTop);
  return (
    <div
      ref={viewport}
      role="region"
      aria-label="Thread history"
      tabIndex={0}
      style={{ overflowY: "auto", overflowAnchor: "none", flex: 1, minHeight: 0 }}
      onWheel={(event) => {
        cancelRestoration();
        const element = event.currentTarget;
        const canScroll =
          (event.deltaY < 0 && element.scrollTop > 0) ||
          (event.deltaY > 0 && element.scrollTop < element.scrollHeight - element.clientHeight);
        if (canScroll) {
          expectUserScroll();
          if (event.deltaY < 0) atBottom.current = false;
        } else if (!scrolledSinceInput.current) {
          captureNextScroll.current = false;
        }
      }}
      onKeyDown={(event) => {
        cancelRestoration();
        if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) expectUserScroll();
        if (["ArrowUp", "PageUp", "Home"].includes(event.key)) atBottom.current = false;
      }}
      onKeyUp={() => {
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onPointerDown={() => {
        cancelRestoration();
        pointerScrolling.current = true;
        expectUserScroll();
      }}
      onPointerUp={() => {
        pointerScrolling.current = false;
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onPointerCancel={() => {
        pointerScrolling.current = false;
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onTouchStart={(event) => {
        cancelRestoration();
        touchY.current = event.touches[0]?.clientY ?? null;
      }}
      onTouchMove={(event) => {
        const next = event.touches[0]?.clientY;
        expectUserScroll();
        if (next !== undefined && touchY.current !== null && next > touchY.current) atBottom.current = false;
        touchY.current = next ?? null;
      }}
      onTouchEnd={() => {
        touchY.current = null;
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onScroll={(event) => {
        const element = event.currentTarget;
        const followed = followPreviousBottom(element);
        recentBottoms.current = [element.scrollHeight - element.clientHeight];
        if (followed) {
          previousScrollTop.current = element.scrollTop;
          return;
        }
        if (element.scrollHeight - element.scrollTop - element.clientHeight < 24) {
          atBottom.current = true;
          cancelRestoration();
        } else if (pointerScrolling.current && element.scrollTop < previousScrollTop.current) atBottom.current = false;
        previousScrollTop.current = element.scrollTop;
        if (restoringAnchor.current !== null) return;
        if (!captureNextScroll.current && !pointerScrolling.current && touchY.current === null) return;
        captureNextScroll.current = true;
        scrolledSinceInput.current = true;
        if (element.scrollTop < LOAD_OLDER_WITHIN) history.loadOlder();
      }}
    >
      {history.loadingOlder && (
        // No height of its own: it floats over the rows without moving any of them.
        <div
          style={{
            position: "sticky",
            top: 0,
            height: 0,
            zIndex: 1,
            display: "flex",
            justifyContent: "center",
            alignItems: "flex-start",
            pointerEvents: "none",
          }}
        >
          <Paper role="status" shadow="xs" radius="xl" px="sm" py={2} mt="xs" withBorder>
            <Text size="xs" c="dimmed">
              Loading earlier…
            </Text>
          </Paper>
        </div>
      )}
      <div ref={contents} style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((item) => {
          const entity = segments[item.index];
          return entity ? (
            <div
              key={item.key}
              data-index={item.index}
              ref={virtualizer.measureElement}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${item.start}px)`,
                paddingBottom: 8,
              }}
            >
              <EntityCard threadId={threadId} entity={entity} live={running && entity.turnId === activeTurn} />
            </div>
          ) : null;
        })}
      </div>
    </div>
  );
}

interface ThreadStatus {
  color: string;
  label: string;
  breathing?: boolean;
}

/** A state shown as a small colored dot rather than a labeled badge: the label is still there for a
 * screen reader, and for anyone hovering or (on a touch/keyboard device) focusing it. `breathing`
 * pulses the dot, for a state that is still settling rather than settled. */
function StatusDot({ color, label, breathing }: ThreadStatus): JSX.Element {
  return (
    <Tooltip label={label} events={{ hover: true, focus: true, touch: true }}>
      <Box
        component="span"
        role="img"
        aria-label={label}
        title={label}
        tabIndex={0}
        className={breathing ? "agentplane-status-dot agentplane-breathing-dot" : "agentplane-status-dot"}
        style={{ backgroundColor: `var(--mantine-color-${color}-6)` }}
      />
    </Tooltip>
  );
}

type Operational = Extract<ThreadEntity["state"], { operational: unknown }>["operational"];

/** The browser's sync of the thread, the runner feed into the server (`operational`) and the
 * harness process are independent state machines; this collapses them into one dot by severity,
 * worst axis first. While the sync is not current, the rest is not either; and an archived thread
 * or an unavailable sandbox makes the retained feed and harness state history, not a live claim. */
function threadStatus({
  sync,
  archived,
  available,
  operational,
  harness,
}: {
  sync: ThreadState;
  archived: boolean;
  available: boolean;
  operational: Operational | null;
  harness: string | null;
}): ThreadStatus {
  if (sync.window?.error) return { color: "red", label: `Thread sync stopped: ${sync.window.error}` };
  if (!sync.window) return { color: "yellow", breathing: true, label: "Connecting…" };
  if (sync.error) return { color: "yellow", breathing: true, label: "Reconnecting…" };
  if (!sync.window.caughtUp) return { color: "yellow", breathing: true, label: "Catching up…" };
  if (archived) return { color: "gray", label: "Thread archived" };
  if (!available) return { color: "gray", label: "Sandbox unavailable" };
  if (operational?.status === "failed") return { color: "red", label: "Runner feed failed" };
  if (harness === "lost") return { color: "red", label: "Harness lost" };
  if (operational?.status === "ended") {
    return { color: "gray", label: `Runner feed ended · harness ${harness ?? "unknown"}` };
  }
  if (harness === null) return { color: "yellow", label: "No harness observed" };
  if (harness === "stopped") return { color: "gray", label: "Runner feed active · harness stopped" };
  return { color: "green", label: `Runner feed active · harness ${harness}` };
}

function ProjectedSessionBody({
  threadId,
  entities,
  thread,
  history,
  available,
}: {
  threadId: string;
  entities: ThreadEntity[];
  thread: ThreadView;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder">;
  available: boolean;
}): JSX.Element {
  const [draft, setDraft] = useState("");
  const sync = useThreadSync().useThread();
  const commands = useProjectedCommands(threadId, entities);
  const view = entities.find((row) => row.entityKind === "view_state");
  const controls = view && "controls" in view.state ? view.state.controls : null;
  const operational = view && "controls" in view.state ? view.state.operational : null;
  const running =
    available && !thread.archived && operational?.status !== "failed" && controls?.harness_state === "running";
  const activeTurn = controls?.active_turn_id ?? null;
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [modelError, setModelError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void models().then(
      (catalog) => {
        if (active) setModelOptions(catalog[thread.harness] ?? []);
      },
      (reason: unknown) => {
        if (active) setModelError(displayableError(reason));
      }
    );
    return () => {
      active = false;
    };
  }, [thread.harness]);
  const segments = entities
    .filter((row) => ["item", "confirmed_input", "lifecycle"].includes(row.entityKind))
    .sort((left, right) =>
      decimalBigInt(left.cursor) < decimalBigInt(right.cursor)
        ? -1
        : decimalBigInt(left.cursor) > decimalBigInt(right.cursor)
          ? 1
          : 0
    );
  const localCommandIds = new Set(commands.local.commands.map((value) => value.command.commandId));
  const projectedCommands = entities.filter(
    (row): row is ThreadEntity & { state: Extract<ThreadEntity["state"], { outcome: string }> } =>
      row.entityKind === "command" && "outcome" in row.state && !localCommandIds.has(row.entityId)
  );
  const hasPendingCommands = projectedCommands.length > 0;
  const selectedCommandIds = commands.local.commands.slice(0, 128);

  // Two Enters before the cleared draft renders would otherwise submit the same text twice, under
  // two command ids. Guards one render, not the lifetime of any HTTP request or command.
  const submitting = useRef(false);
  useEffect(() => {
    submitting.current = false;
  }, [draft]);

  function submit(): void {
    if (!draft.trim() || !running || submitting.current) return;
    submitting.current = true;
    const value = create(CommandSchema, {
      commandId: crypto.randomUUID(),
      operation: { case: "submitInput", value: { text: draft } },
    });
    if (commands.submit(value)) setDraft("");
    else submitting.current = false;
  }

  function composerKey(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (!(event.ctrlKey || event.metaKey)) {
      submit();
      return;
    }
    // Insert the newline by hand: a textarea ignores Ctrl+Enter, and setting a controlled value
    // leaves the caret at the end, so put it back where the newline went.
    const field = event.currentTarget;
    const at = field.selectionStart;
    setDraft(`${draft.slice(0, at)}\n${draft.slice(field.selectionEnd)}`);
    requestAnimationFrame(() => field.setSelectionRange(at + 1, at + 1));
  }

  return (
    <RetainedDisclosureProvider>
      <Stack
        style={{ flex: 1, minHeight: 0 }}
        data-projection-cursor={view ? decimalBigInt(view.revisionCursor).toString() : undefined}
      >
        <VirtualizedHistory
          threadId={threadId}
          segments={segments}
          running={running}
          activeTurn={activeTurn}
          history={history}
        />
        {hasPendingCommands && (
          <Stack role="region" aria-label="Pending commands" gap="xs">
            {projectedCommands.map((row) => (
              <Paper
                key={row.entityId}
                data-command-id={row.entityId}
                p="xs"
                withBorder
                style={{ position: "relative" }}
              >
                <EvidenceToggle entity={row} style={{ position: "absolute", top: 4, right: 4 }} />
                <Text size="xs" c={row.pending ? "dimmed" : row.state.outcome === "failed" ? "red" : undefined}>
                  {row.state.outcome === "pending"
                    ? "Saved · awaiting effect"
                    : commandOutcomeLabel(row.state.operation, row.state.outcome)}
                  {row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}
                </Text>
                {row.inputRef && <Body reference={row.inputRef} format="text" />}
                <EvidencePanel threadId={threadId} entity={row} />
              </Paper>
            ))}
          </Stack>
        )}
        {selectedCommandIds.length > 0 && (
          <SelectedCommandOutcomes
            commands={selectedCommandIds}
            store={commands.store}
            errors={commands.errors}
            deliver={commands.deliver}
          />
        )}
        {operational?.feed_error && (
          <Text role="alert" c="red">
            {operational.feed_error.cursor === null
              ? `Projection failed: ${operational.feed_error.message}. Showing verified history through event ${operational.last_verified_cursor}.`
              : `Rejected event ${operational.feed_error.cursor}: ${operational.feed_error.message}. Showing verified history through event ${operational.last_verified_cursor}.`}
          </Text>
        )}
        {modelError && (
          <Text role="alert" c="red">
            {modelError}
          </Text>
        )}
        {commands.submissionError && (
          <Text role="alert" c="red">
            {commands.submissionError}
          </Text>
        )}
        <Textarea
          value={draft}
          onChange={(event) => setDraft(event.currentTarget.value)}
          placeholder="Enter sends, Ctrl+Enter for a new line"
          autosize
          minRows={2}
          maxRows={12}
          disabled={!running}
          onKeyDown={composerKey}
        />
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <StatusDot
              {...threadStatus({
                sync,
                archived: thread.archived,
                available,
                operational,
                harness: controls?.harness_state ?? null,
              })}
            />
            <Select
              aria-label="Model"
              data={modelOptions}
              value={controls?.applied_model ?? null}
              placeholder={
                sync.window?.error || operational?.status === "failed"
                  ? "Model unavailable"
                  : !sync.window?.caughtUp
                    ? "Catching up…"
                    : "Model"
              }
              disabled={!running}
              w={200}
              onChange={(model) =>
                model &&
                commands.submit(
                  create(CommandSchema, {
                    commandId: crypto.randomUUID(),
                    operation: { case: "changeModel", value: { model } },
                  })
                )
              }
            />
          </Group>
          <Group gap="xs" wrap="nowrap">
            {/* Opens upward: the composer sits at the bottom of the viewport. */}
            <Menu position="top-end" withArrow shadow="md">
              <Menu.Target>
                <ActionIcon size="lg" variant="light" aria-label="More">
                  <IconDotsVertical size={16} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item
                  color="red"
                  leftSection={<IconPower size={15} />}
                  disabled={!running}
                  onClick={() =>
                    commands.submit(
                      create(CommandSchema, {
                        commandId: crypto.randomUUID(),
                        operation: { case: "stopRunnerSession", value: {} },
                      })
                    )
                  }
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
              disabled={!running || !activeTurn}
              onClick={() =>
                activeTurn &&
                commands.submit(
                  create(CommandSchema, {
                    commandId: crypto.randomUUID(),
                    operation: { case: "interruptTurn", value: { turnId: activeTurn } },
                  })
                )
              }
            >
              <IconPlayerStop size={16} />
            </ActionIcon>
            <ActionIcon size="lg" aria-label="Send" disabled={!running || !draft.trim()} onClick={submit}>
              <IconSend size={16} />
            </ActionIcon>
          </Group>
        </Group>
      </Stack>
    </RetainedDisclosureProvider>
  );
}

function SyncedThread({
  threadId,
  thread,
  available,
}: {
  threadId: string;
  thread: ThreadView;
  available: boolean;
}): JSX.Element {
  const { window: shown, error } = useThreadSync().useThread();
  if (!shown) {
    if (error) return <p role="alert">Thread sync failed: {error}</p>;
    return <p role="status">Loading thread…</p>;
  }
  return (
    <>
      {error && <p role="alert">Thread sync failed: {error}; showing the current window and retrying.</p>}
      {shown.error && (
        <p role="alert">
          Thread synchronization stopped: {shown.error} <button onClick={shown.refresh}>Refresh thread</button>
        </p>
      )}
      {!shown.error && !shown.caughtUp && (
        <p role="status" data-thread-catchup="true">
          Catching up thread…
        </p>
      )}
      <ProjectedSessionBody
        threadId={threadId}
        entities={shown.caughtUp ? shown.rows : []}
        thread={thread}
        history={shown}
        available={available}
      />
    </>
  );
}

export function ProjectedSession({ threadId, onBack }: { threadId: string; onBack: () => void }): JSX.Element {
  const sync = useThreadSync();
  const [thread, setThread] = useState<ThreadView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const environment = useLive<SandboxesSnapshot>(liveSandboxesUrl());
  const sandboxAvailable =
    environment.connection === "connected" &&
    environment.health?.fresh === true &&
    environment.snapshot?.sandboxes.some(
      (sandbox) => sandbox.name === thread?.sandbox && sandbox.state === "running"
    ) === true;
  useEffect(() => {
    void getThread(threadId).then(setThread, (reason: unknown) => setError(displayableError(reason)));
  }, [threadId]);
  return (
    <ChronologicalDebugProvider key={threadId} threadId={threadId}>
      <Stack style={{ flex: 1, minHeight: 0 }}>
        <Group>
          <Button variant="subtle" onClick={onBack}>
            ← Threads
          </Button>
          <ChronologicalDebugLink />
          <ThreadTitle threadId={threadId} thread={thread} onRenamed={setThread} onError={setError} />
        </Group>
        {error && (
          <Text role="alert" c="red">
            {error}
          </Text>
        )}
        {thread && (
          <Text size="xs" c="dimmed">
            {thread.sandbox}
          </Text>
        )}
        {thread?.archived && (
          <Text role="status" c="dimmed">
            Thread archived. Showing retained thread history; controls are disabled.
          </Text>
        )}
        {thread &&
          environment.snapshot &&
          !environment.snapshot.sandboxes.some((sandbox) => sandbox.name === thread.sandbox) && (
            <Text role="status" c="dimmed">
              Sandbox no longer exists. Showing archived Thread history; controls are disabled.
            </Text>
          )}
        {thread && (
          <sync.Thread key={threadId} threadId={threadId}>
            <SyncedThread threadId={threadId} thread={thread} available={sandboxAvailable} />
          </sync.Thread>
        )}
      </Stack>
    </ChronologicalDebugProvider>
  );
}
