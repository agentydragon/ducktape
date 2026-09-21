import { ActionIcon, Badge, Button, Group, Paper, Select, Stack, Text, Textarea, TextInput } from "@mantine/core";
import { create, fromJson, type JsonValue } from "@bufbuild/protobuf";
import { useVirtualizer } from "@tanstack/react-virtual";
import IconPlayerStop from "@tabler/icons-react/dist/esm/icons/IconPlayerStop.mjs";
import { type JSX, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventSchema, ItemKind, TurnStatus } from "../../protocol/event_pb";
import {
  command,
  conversationEvidence,
  conversationFrames,
  displayableError,
  getThread,
  models,
  renameThread,
  type EvidencePage,
  type NativeFramePage,
  type ThreadView,
} from "./client";
import {
  ConversationCollection,
  CommandSelection,
  decimalBigInt,
  PendingCommandPages,
  PayloadBody,
  type ConversationEntity,
  type PayloadRef,
} from "./conversation_store";
import { LocalCommands, type LocalCommand, type LocalCommandSnapshot } from "./local_commands";
import { liveSandboxesUrl, useLive, type SandboxesSnapshot } from "./live";
import { Markdown } from "./markdown";
import { RetainedDisclosure, RetainedDisclosureProvider } from "./retained_disclosures";
import { ChronologicalDebugLink, ChronologicalDebugProvider } from "./chronological_debug";

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

function Body({
  threadId,
  reference,
  follow,
  plain = false,
}: {
  threadId: string;
  reference: PayloadRef | null;
  follow: boolean;
  plain?: boolean;
}): JSX.Element {
  if (!reference) return <Text c="dimmed">Body not observed</Text>;
  return (
    <PayloadBody threadId={threadId} reference={reference} follow={follow}>
      {(body) =>
        body === null ? (
          <Text c="dimmed">Loading complete revision…</Text>
        ) : plain ? (
          <Text component="pre" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {body}
          </Text>
        ) : (
          <Markdown source={body} />
        )
      }
    </PayloadBody>
  );
}

function LazyBody({
  label,
  ...body
}: {
  label: string;
  threadId: string;
  reference: PayloadRef;
  follow: boolean;
  plain?: boolean;
}): JSX.Element {
  const id = `${body.reference.source_id}:${body.reference.projection_epoch}:${body.reference.owner_item_id}:${body.reference.field}`;
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
  entity: ConversationEntity;
  observationCursor: string;
}): JSX.Element {
  const [page, setPage] = useState<NativeFramePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [afterSequence, setAfterSequence] = useState("0");
  const request = useRef<AbortController | null>(null);
  const scope = {
    sourceId: entity.sourceId,
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
    void conversationFrames(threadId, scope, observationCursor, after, controller.signal)
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

function EvidenceFrames(props: {
  threadId: string;
  entity: ConversationEntity;
  observationCursor: string;
}): JSX.Element {
  const { entity } = props;
  const id = `${entity.sourceId}:${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:frames:${props.observationCursor}`;
  return (
    <RetainedDisclosure id={id} summary={`Observation ${props.observationCursor} raw frames`}>
      <EvidenceFramesPage key={id} {...props} />
    </RetainedDisclosure>
  );
}

function EvidencePageView({ threadId, entity }: { threadId: string; entity: ConversationEntity }): JSX.Element {
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [afterCursor, setAfterCursor] = useState("0");
  const request = useRef<AbortController | null>(null);
  const scope = {
    sourceId: entity.sourceId,
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
    void conversationEvidence(threadId, scope, after, controller.signal)
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

function Evidence({ threadId, entity }: { threadId: string; entity: ConversationEntity }): JSX.Element {
  const id = `${entity.sourceId}:${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:evidence`;
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
  entity: ConversationEntity;
  live: boolean;
}): JSX.Element {
  if (entity.entityKind === "confirmed_input") {
    return (
      <Group justify="flex-end" data-conversation-anchor={entity.cursor.toString()}>
        <Paper className="agentplane-user-bubble" p="sm" withBorder maw="80%">
          <Body threadId={threadId} reference={entity.inputRef} follow={false} />
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
      <Stack gap="xs" data-conversation-anchor={entity.cursor.toString()}>
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
    <Paper p="sm" withBorder data-conversation-anchor={entity.cursor.toString()}>
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
        (reasoning ? (
          <LazyBody label="Reasoning" threadId={threadId} reference={entity.textRef} follow={streaming} />
        ) : (
          <Body threadId={threadId} reference={entity.textRef} follow={streaming} />
        ))}
      {entity.argumentsRef && (
        <LazyBody label="Arguments" threadId={threadId} reference={entity.argumentsRef} follow={streaming} plain />
      )}
      {entity.outputRef && (
        <LazyBody label="Output" threadId={threadId} reference={entity.outputRef} follow={streaming} plain />
      )}
      <Evidence threadId={threadId} entity={entity} />
    </Paper>
  );
}

function useProjectedCommands(threadId: string, entities: ConversationEntity[]) {
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
  threadId,
  sourceId,
  projectionEpoch,
  commands,
  store,
  errors,
  deliver,
}: {
  threadId: string;
  sourceId: string;
  projectionEpoch: string;
  commands: LocalCommand[];
  store: LocalCommands;
  errors: ReadonlyMap<string, string>;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element {
  const ids = commands.slice(0, 128).map((value) => value.command.commandId);
  return (
    <CommandSelection threadId={threadId} sourceId={sourceId} projectionEpoch={projectionEpoch} commandIds={ids}>
      {(rows) => (
        <SelectedCommandRows
          threadId={threadId}
          rows={rows}
          commands={commands}
          store={store}
          errors={errors}
          deliver={deliver}
        />
      )}
    </CommandSelection>
  );
}

function SelectedCommandRows({
  threadId,
  rows,
  commands,
  store,
  errors,
  deliver,
}: {
  threadId: string;
  rows: ConversationEntity[];
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
                {row.inputRef && <Body threadId={threadId} reference={row.inputRef} follow={false} />}
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

function PendingCommandRows({
  threadId,
  current,
  older,
  unresolvedCount,
  canLoadOlder,
  hasOlder,
  loadOlder,
  clearOlder,
  excludedIds,
}: {
  threadId: string;
  current: ConversationEntity[];
  older: ConversationEntity[];
  unresolvedCount: number;
  canLoadOlder: boolean;
  hasOlder: boolean;
  loadOlder: () => void;
  clearOlder: () => void;
  excludedIds: ReadonlySet<string>;
}): JSX.Element {
  const rows = new Map<string, ConversationEntity>();
  for (const row of [...current, ...older]) {
    const existing = rows.get(row.entityId);
    if (
      !excludedIds.has(row.entityId) &&
      (existing === undefined || decimalBigInt(row.revisionCursor) > decimalBigInt(existing.revisionCursor))
    )
      rows.set(row.entityId, row);
  }
  return (
    <Stack role="region" aria-label="Command updates" gap="xs">
      <Text size="sm">Command updates · {unresolvedCount} pending</Text>
      {[...rows.values()].map((row) => {
        if (!("outcome" in row.state)) return null;
        return (
          <Paper key={row.entityId} data-command-id={row.entityId} p="xs" withBorder>
            <Text size="xs" c={row.pending ? "dimmed" : row.state.outcome === "failed" ? "red" : undefined}>
              {row.state.outcome === "pending"
                ? "Saved · awaiting effect"
                : commandOutcomeLabel(row.state.operation, row.state.outcome)}
              {row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}
            </Text>
            {row.inputRef && <Body threadId={threadId} reference={row.inputRef} follow={false} />}
            <Evidence threadId={threadId} entity={row} />
          </Paper>
        );
      })}
      {canLoadOlder && <Button onClick={loadOlder}>Load 30 older pending commands</Button>}
      {hasOlder && (
        <Button variant="subtle" onClick={clearOlder}>
          Show current pending commands
        </Button>
      )}
    </Stack>
  );
}

function VirtualizedHistory({
  threadId,
  segments,
  running,
  activeTurn,
  onLoadOlder,
}: {
  threadId: string;
  segments: ConversationEntity[];
  running: boolean;
  activeTurn: string | null;
  onLoadOlder: (cursor: string) => void;
}): JSX.Element {
  const viewport = useRef<HTMLDivElement>(null);
  const contents = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const previousScrollTop = useRef(0);
  const previousScrollHeight = useRef(0);
  const previousClientHeight = useRef(0);
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
  const requestedBefore = useRef<string | null>(null);
  const cancelRestoration = () => {
    if (restorationFrame.current !== null) cancelAnimationFrame(restorationFrame.current);
    restorationFrame.current = null;
    restorationSize.current = null;
    restoringAnchor.current = null;
  };
  const followPreviousBottom = (element: HTMLDivElement) => {
    // A programmatic return to the old bottom can be delivered after a card grows. Preserve
    // it before restoring a stale reader anchor, while an explicit user gesture owns its scroll.
    if (captureNextScroll.current) return false;
    const previousBottom = previousScrollHeight.current - previousClientHeight.current;
    if (Math.abs(element.scrollTop - previousBottom) > 2) return false;
    atBottom.current = true;
    cancelRestoration();
    element.scrollTop = element.scrollHeight;
    return true;
  };
  function correctRestoration(): number | null {
    const anchor = readingAnchor.current;
    const element = viewport.current;
    if (!anchor || !element || restoringAnchor.current !== anchor.key) return null;
    const row = element.querySelector<HTMLElement>(`[data-conversation-anchor="${anchor.cursor}"]`);
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
    const first = [...element.querySelectorAll<HTMLElement>("[data-conversation-anchor]")].find(
      (candidate) => candidate.getBoundingClientRect().bottom > viewportTop
    );
    const firstEntity = first
      ? segments.find((entity) => entity.cursor.toString() === first.dataset.conversationAnchor)
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
      const row = element?.querySelector<HTMLElement>(`[data-conversation-anchor="${anchor.cursor}"]`);
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
    previousScrollHeight.current = element.scrollHeight;
    previousClientHeight.current = element.clientHeight;
    const observer = new ResizeObserver(() => {
      // A scrollbar drag or programmatic equivalent can reach the old bottom in the same task
      // that grows the last card, before the browser dispatches its scroll event. Preserve that
      // user choice across the resize without interpreting arbitrary layout movement as intent.
      if (followPreviousBottom(element) || atBottom.current) element.scrollTop = element.scrollHeight;
      // Content can resize while a wheel, touch, or key scroll is still settling. Its
      // measured rows do not describe the reader's final position yet; scrollend will
      // capture that position before a later resize restoration is eligible.
      else if (!captureNextScroll.current && readingAnchor.current) restoreAnchor(readingAnchor.current, true);
      previousScrollHeight.current = element.scrollHeight;
      previousClientHeight.current = element.clientHeight;
    });
    observer.observe(content);
    return () => {
      observer.disconnect();
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
        if (followPreviousBottom(element)) {
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
        // Retain one segment across adjacent reading windows so the virtualizer can restore
        // the same measured item and pixel offset after the old collection is evicted.
        const boundary = (segments[1] ?? segments[0])?.cursor.toString();
        if (element.scrollTop < 80 && boundary && requestedBefore.current !== boundary) {
          requestedBefore.current = boundary;
          onLoadOlder(boundary);
        }
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

function ProjectedSessionBody({
  threadId,
  entities,
  thread,
  onLoadOlder,
  available,
}: {
  threadId: string;
  entities: ConversationEntity[];
  thread: ThreadView;
  onLoadOlder: (cursor: string) => void;
  available: boolean;
}): JSX.Element {
  const [draft, setDraft] = useState("");
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
  const selectedCommandIds = commands.local.commands.slice(0, 128);

  function submit(): void {
    if (!draft.trim() || !running) return;
    const value = create(CommandSchema, {
      commandId: crypto.randomUUID(),
      operation: { case: "submitInput", value: { text: draft } },
    });
    if (commands.submit(value)) setDraft("");
  }

  return (
    <RetainedDisclosureProvider>
      <Stack
        style={{ flex: 1, minHeight: 0 }}
        data-projection-cursor={view ? decimalBigInt(view.revisionCursor).toString() : undefined}
      >
        <Button
          variant="subtle"
          disabled={!segments.length}
          onClick={() => {
            const boundary = segments[1] ?? segments[0];
            if (boundary) onLoadOlder(boundary.cursor.toString());
          }}
        >
          Load 30 earlier
        </Button>
        <VirtualizedHistory
          threadId={threadId}
          segments={segments}
          running={running}
          activeTurn={activeTurn}
          onLoadOlder={onLoadOlder}
        />
        {view && "controls" in view.state && (
          <PendingCommandPages
            key={`${view.sourceId}:${view.projectionEpoch}`}
            threadId={threadId}
            sourceId={view.sourceId}
            projectionEpoch={view.projectionEpoch}
            viewRevisionCursor={view.revisionCursor}
            commandRevisionCursor={view.state.command_revision_cursor}
          >
            {(page) => (
              <PendingCommandRows
                threadId={threadId}
                excludedIds={new Set(selectedCommandIds.map((value) => value.command.commandId))}
                {...page}
              />
            )}
          </PendingCommandPages>
        )}
        {view && selectedCommandIds.length > 0 && (
          <SelectedCommandOutcomes
            threadId={threadId}
            sourceId={view.sourceId}
            projectionEpoch={view.projectionEpoch}
            commands={selectedCommandIds}
            store={commands.store}
            errors={commands.errors}
            deliver={commands.deliver}
          />
        )}
        {!view && selectedCommandIds.length > 0 && (
          <SelectedCommandRows
            threadId={threadId}
            rows={[]}
            commands={selectedCommandIds}
            store={commands.store}
            errors={commands.errors}
            deliver={commands.deliver}
          />
        )}
        {operational?.feed_error && (
          <Text role="alert" c="red">
            Rejected event {operational.feed_error.cursor}: {operational.feed_error.message}. Showing verified history
            through event {operational.last_verified_cursor}.
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
          disabled={!running}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.ctrlKey && !event.metaKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <Button disabled={!running || !draft.trim()} onClick={submit}>
          Send
        </Button>
        <Group justify="space-between" wrap="nowrap">
          <Select
            aria-label="Model"
            data={modelOptions}
            value={controls?.applied_model ?? null}
            disabled={!running}
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
          <Group gap="xs">
            <Button
              color="red"
              variant="subtle"
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
            </Button>
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
          </Group>
        </Group>
      </Stack>
    </RetainedDisclosureProvider>
  );
}

export function ProjectedSession({ threadId, onBack }: { threadId: string; onBack: () => void }): JSX.Element {
  const [thread, setThread] = useState<ThreadView | null>(null);
  const [before, setBefore] = useState<string | undefined>();
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
            Thread archived. Showing retained conversation history; controls are disabled.
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
          <ConversationCollection key={threadId} threadId={threadId} beforeCursor={before}>
            {(rows) => (
              <ProjectedSessionBody
                threadId={threadId}
                entities={rows}
                thread={thread}
                onLoadOlder={setBefore}
                available={sandboxAvailable}
              />
            )}
          </ConversationCollection>
        )}
      </Stack>
    </ChronologicalDebugProvider>
  );
}
