import {
  ActionIcon,
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
  TextInput,
  Tooltip,
} from "@mantine/core";
import { create, fromJson, type JsonValue } from "@bufbuild/protobuf";
import { useVirtualizer } from "@tanstack/react-virtual";
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import IconPlayerStop from "@tabler/icons-react/dist/esm/icons/IconPlayerStop.mjs";
import IconPower from "@tabler/icons-react/dist/esm/icons/IconPower.mjs";
import IconSend from "@tabler/icons-react/dist/esm/icons/IconSend.mjs";
import {
  type JSX,
  type KeyboardEvent,
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
  renameThread,
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
import { Markdown } from "./markdown";
import { RetainedDisclosure, RetainedDisclosureProvider } from "./retained_disclosures";
import { ChronologicalDebugLink, ChronologicalDebugProvider } from "./chronological_debug";
import "./projected_session.css";

const EMPTY_LOCAL: LocalCommandSnapshot = { commands: [], error: null };

export function pruneCommandErrors(errors: Map<string, string>, commandIds: ReadonlySet<string>): Map<string, string> {
  if (Array.from(errors.keys()).every((id) => commandIds.has(id))) return errors;
  return new Map(Array.from(errors).filter(([id]) => commandIds.has(id)));
}

const LIFECYCLE_LABELS: Record<string, string> = {
  turn_started: "Turn started",
  turn_completed: "Turn completed",
  model_changed: "Model changed",
  harness_started: "Harness started",
  harness_exited: "Harness exited",
  harness_lost: "Harness connection lost",
};

function lifecyclePresentation(observation: string, event: unknown): { label: string; diagnostic: string | null } {
  const parsed = event === null || event === undefined ? null : fromJson(EventSchema, event as JsonValue);
  const completed = parsed?.observation.case === "turnCompleted" ? parsed.observation.value : null;
  const diagnostic = completed?.error || null;
  if (completed?.status === TurnStatus.FAILED) return { label: "Turn failed", diagnostic };
  if (completed?.status === TurnStatus.INTERRUPTED) return { label: "Turn interrupted", diagnostic };
  return { label: LIFECYCLE_LABELS[observation] ?? observation.replaceAll("_", " "), diagnostic };
}

function Body({ reference, plain = false }: { reference: PayloadRef | null; plain?: boolean }): JSX.Element {
  if (!reference) return <Text c="dimmed">Body not observed</Text>;
  return <PayloadText reference={reference} plain={plain} />;
}

function PayloadText({ reference, plain }: { reference: PayloadRef; plain: boolean }): JSX.Element {
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
      ) : plain ? (
        <Text component="pre" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {body}
        </Text>
      ) : (
        <Markdown source={body} />
      )}
    </>
  );
}

function LazyBody({ label, ...body }: { label: string; reference: PayloadRef; plain?: boolean }): JSX.Element {
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
      {page?.frames.map((frame) => (
        <Text
          component="pre"
          size="xs"
          key={frame.source_sequence}
          style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
        >
          {frame.availability === "present"
            ? JSON.stringify(frame.entry, null, 2)
            : `Raw frame ${frame.source_sequence} unavailable`}
        </Text>
      ))}
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
        <Stack gap="xs" key={observation.observation_cursor}>
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

function Evidence({ threadId, entity }: { threadId: string; entity: ThreadEntity }): JSX.Element {
  const id = `${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:evidence`;
  return (
    <RetainedDisclosure id={id} summary="Evidence">
      <EvidencePageView key={id} threadId={threadId} entity={entity} />
    </RetainedDisclosure>
  );
}

function EntityCard({
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
      <Group justify="flex-end" data-thread-anchor={entity.cursor.toString()}>
        <Paper className="agentplane-user-bubble" p="sm" withBorder maw="80%">
          <Body reference={entity.inputRef} />
          <Evidence threadId={threadId} entity={entity} />
        </Paper>
      </Group>
    );
  }
  if (entity.entityKind === "lifecycle") {
    const observation = "observation" in entity.state ? entity.state.observation : "lifecycle";
    const event = "event" in entity.state ? entity.state.event : null;
    const presentation = lifecyclePresentation(observation, event);
    return (
      <Stack gap="xs" data-thread-anchor={entity.cursor.toString()}>
        <Text size="xs" c={presentation.diagnostic || observation === "harness_lost" ? "red" : "dimmed"}>
          {presentation.label}
        </Text>
        {presentation.diagnostic && (
          <Text c="red" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {presentation.diagnostic}
          </Text>
        )}
        {"event" in entity.state && (
          <details>
            <summary>Lifecycle details</summary>
            <Text component="pre" size="xs" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {JSON.stringify(entity.state.event, null, 2)}
            </Text>
          </details>
        )}
        <Evidence threadId={threadId} entity={entity} />
      </Stack>
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
      </Group>
      {entity.textRef &&
        (reasoning ? <LazyBody label="Reasoning" reference={entity.textRef} /> : <Body reference={entity.textRef} />)}
      {entity.argumentsRef && <LazyBody label="Arguments" reference={entity.argumentsRef} plain />}
      {entity.outputRef && <LazyBody label="Output" reference={entity.outputRef} plain />}
      <Evidence threadId={threadId} entity={entity} />
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

  async function deliver(value: LocalCommand): Promise<void> {
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
  }
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
                {row.inputRef && <Body reference={row.inputRef} />}
                <Button variant="subtle" onClick={() => store.dismiss(row.entityId)}>
                  Dismiss
                </Button>
              </>
            ) : (
              <>
                <Text size="sm">{admitted ? "Saved · awaiting effect" : "Saved locally · awaiting admission"}</Text>
                {value.command.operation.case === "submitInput" && (
                  <Markdown source={value.command.operation.value.text} />
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

function VirtualizedHistory({
  threadId,
  segments,
  running,
  activeTurn,
  onLoadOlder,
}: {
  threadId: string;
  segments: ThreadEntity[];
  running: boolean;
  activeTurn: string | null;
  onLoadOlder: () => void;
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
    };
    element.addEventListener("scrollend", onScrollEnd);
    return () => element.removeEventListener("scrollend", onScrollEnd);
  }, [segments]);
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
        if (element.scrollTop < 80) onLoadOlder();
      }}
    >
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
 * worst axis first. While the sync is not current, the other two are not either. */
function threadStatus(sync: ThreadState, operational: Operational | null, harness: string | null): ThreadStatus {
  if (sync.window?.error) return { color: "red", label: `Thread sync stopped: ${sync.window.error}` };
  if (!sync.window) return { color: "yellow", breathing: true, label: "Connecting…" };
  if (sync.error) return { color: "yellow", breathing: true, label: "Reconnecting…" };
  if (!sync.window.caughtUp) return { color: "yellow", breathing: true, label: "Catching up…" };
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
  history: Pick<ThreadWindow, "olderAvailable" | "loadOlder">;
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

  function submit(): void {
    if (!draft.trim() || !running) return;
    const value = create(CommandSchema, {
      commandId: crypto.randomUUID(),
      operation: { case: "submitInput", value: { text: draft } },
    });
    if (commands.submit(value)) setDraft("");
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
        <Button variant="subtle" disabled={!history.olderAvailable} onClick={history.loadOlder}>
          Load 30 earlier
        </Button>
        <VirtualizedHistory
          threadId={threadId}
          segments={segments}
          running={running}
          activeTurn={activeTurn}
          onLoadOlder={history.loadOlder}
        />
        {hasPendingCommands && (
          <Stack role="region" aria-label="Pending commands" gap="xs">
            {projectedCommands.map((row) => (
              <Paper key={row.entityId} data-command-id={row.entityId} p="xs" withBorder>
                <Text size="xs" c={row.pending ? "dimmed" : row.state.outcome === "failed" ? "red" : undefined}>
                  {row.state.outcome === "pending"
                    ? "Saved · awaiting effect"
                    : commandOutcomeLabel(row.state.operation, row.state.outcome)}
                  {row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}
                </Text>
                {row.inputRef && <Body reference={row.inputRef} />}
                <Evidence threadId={threadId} entity={row} />
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
            <StatusDot {...threadStatus(sync, operational, controls?.harness_state ?? null)} />
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
                <ActionIcon variant="light" aria-label="More">
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
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const environment = useLive<SandboxesSnapshot>(liveSandboxesUrl());
  const sandboxAvailable =
    environment.connection === "connected" &&
    environment.health?.fresh === true &&
    environment.snapshot?.sandboxes.some(
      (sandbox) => sandbox.name === thread?.sandbox && sandbox.state === "running"
    ) === true;
  useEffect(() => {
    void getThread(threadId).then(
      (value) => {
        setThread(value);
        setName(value.name ?? "");
      },
      (reason: unknown) => setError(displayableError(reason))
    );
  }, [threadId]);
  return (
    <ChronologicalDebugProvider key={threadId} threadId={threadId}>
      <Stack style={{ flex: 1, minHeight: 0 }}>
        <Group>
          <Button variant="subtle" onClick={onBack}>
            ← Threads
          </Button>
          <ChronologicalDebugLink />
          <TextInput
            aria-label="Thread name"
            value={name}
            placeholder={threadId}
            onChange={(event) => setName(event.currentTarget.value)}
            onBlur={() =>
              void renameThread(threadId, name.trim() || null).then(setThread, (reason: unknown) =>
                setError(displayableError(reason))
              )
            }
          />
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
