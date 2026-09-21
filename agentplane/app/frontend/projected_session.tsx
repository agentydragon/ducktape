import { ActionIcon, Badge, Button, Group, Paper, Select, Stack, Text, Textarea, TextInput } from "@mantine/core";
import { create } from "@bufbuild/protobuf";
import { useVirtualizer } from "@tanstack/react-virtual";
import IconPlayerStop from "@tabler/icons-react/dist/esm/icons/IconPlayerStop.mjs";
import { type JSX, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { ItemKind } from "../../protocol/event_pb";
import {
  command,
  conversationEvidence,
  conversationFrames,
  displayableError,
  getThread,
  models,
  reconcileCommands,
  renameThread,
  type EvidencePage,
  type NativeFramePage,
  type ThreadView,
} from "./client";
import {
  ConversationCollection,
  decimalBigInt,
  PayloadBody,
  type ConversationEntity,
  type PayloadRef,
} from "./conversation_store";
import { LocalCommands, type LocalCommand, type LocalCommandSnapshot } from "./local_commands";
import { liveSandboxesUrl, useLive, type SandboxesSnapshot } from "./live";
import { Markdown } from "./markdown";
import { RetainedDisclosure, RetainedDisclosureProvider } from "./retained_disclosures";

const EMPTY_LOCAL: LocalCommandSnapshot = { commands: [], error: null };
const LIFECYCLE_LABELS: Record<string, string> = {
  turn_started: "Turn started",
  turn_completed: "Turn completed",
  model_changed: "Model changed",
  harness_started: "Harness started",
  harness_exited: "Harness exited",
  harness_lost: "Harness connection lost",
};

function lifecyclePresentation(observation: string, event: unknown): { label: string; diagnostic: string | null } {
  if (!event || typeof event !== "object")
    return { label: LIFECYCLE_LABELS[observation] ?? observation, diagnostic: null };
  const fields = event as Record<string, unknown>;
  const status = typeof fields.status === "string" ? fields.status : null;
  const diagnostic = typeof fields.error === "string" && fields.error ? fields.error : null;
  if (observation === "turn_completed" && status?.endsWith("FAILED")) return { label: "Turn failed", diagnostic };
  if (observation === "turn_completed" && status?.endsWith("INTERRUPTED"))
    return { label: "Turn interrupted", diagnostic };
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
  const [open, setOpen] = useState(false);
  return (
    <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Observation {props.observationCursor} raw frames</summary>
      {open && <EvidenceFramesPage {...props} />}
    </details>
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
      {page?.observations.map((observation) =>
        observation.has_native ? (
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
        )
      )}
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
  const [open, setOpen] = useState(false);
  return (
    <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Evidence</summary>
      {open && <EvidencePageView threadId={threadId} entity={entity} />}
    </details>
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
  const active = useRef(new Set<string>());
  const commandIds = useMemo(
    () =>
      new Set([
        ...entities.filter((row) => row.entityKind === "command").map((row) => row.entityId),
        ...entities.flatMap((row) => ("origin_command_ids" in row.state ? row.state.origin_command_ids : [])),
      ]),
    [entities]
  );
  const [reconcileAttempt, setReconcileAttempt] = useState(0);
  const localCommandKey = local.commands
    .slice(0, 128)
    .map((value) => value.command.commandId)
    .join("\u0000");
  useEffect(() => store.observeCommandIds(commandIds), [commandIds, store]);
  const scope = entities.find((row) => row.entityKind === "view_state");
  const sourceId = scope?.sourceId;
  const projectionEpoch = scope?.projectionEpoch;
  useEffect(() => setReconcileAttempt(0), [localCommandKey, projectionEpoch, sourceId]);
  useEffect(() => {
    if (!sourceId || !projectionEpoch || !localCommandKey) return;
    const controller = new AbortController();
    let retry: number | undefined;
    void reconcileCommands(
      threadId,
      sourceId,
      projectionEpoch,
      localCommandKey.split("\u0000"),
      controller.signal
    ).then(
      (result) => {
        if (!controller.signal.aborted) {
          store.observeCommandIds(
            new Set(result.commands.filter((value) => value.outcome !== null).map((value) => value.command_id))
          );
          if (result.commands.some((value) => value.outcome === null)) {
            retry = window.setTimeout(
              () => setReconcileAttempt((value) => value + 1),
              Math.min(5_000, 250 * 2 ** reconcileAttempt)
            );
          }
        }
      },
      (reason: unknown) => {
        if (!controller.signal.aborted)
          setErrors((previous) => new Map(previous).set("reconciliation", displayableError(reason)));
      }
    );
    return () => {
      controller.abort();
      if (retry !== undefined) window.clearTimeout(retry);
    };
  }, [localCommandKey, projectionEpoch, reconcileAttempt, sourceId, store, threadId]);

  async function deliver(value: LocalCommand): Promise<void> {
    const id = value.command.commandId;
    if (active.current.has(id)) return;
    active.current.add(id);
    try {
      store.acknowledge(value.command, await command(threadId, value.command));
    } catch (reason) {
      setErrors((previous) => new Map(previous).set(id, displayableError(reason)));
    } finally {
      active.current.delete(id);
    }
  }
  function submit(value: Command): boolean {
    try {
      void deliver(store.remember(value));
      return true;
    } catch (reason) {
      setErrors((previous) => new Map(previous).set(value.commandId, displayableError(reason)));
      return false;
    }
  }
  return { local, errors, submit, deliver };
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
  const previousCount = useRef(segments.length);
  const previousFirstKey = useRef<string | null>(null);
  const readingAnchor = useRef<{ key: string; offset: number } | null>(null);
  const requestedBefore = useRef<string | null>(null);
  const virtualizer = useVirtualizer({
    count: segments.length,
    getScrollElement: () => viewport.current,
    estimateSize: () => 180,
    getItemKey: (index) => `${segments[index]?.entityKind}:${segments[index]?.entityId}`,
    measureElement: (element) => element.getBoundingClientRect().height,
    overscan: 5,
  });
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
      if (index >= 0) {
        virtualizer.scrollToIndex(index, { align: "start" });
        requestAnimationFrame(() => {
          if (viewport.current && readingAnchor.current) viewport.current.scrollTop += readingAnchor.current.offset;
        });
      }
    }
    previousCount.current = segments.length;
    previousFirstKey.current = firstKey;
  }, [segments, virtualizer]);
  useLayoutEffect(() => {
    const element = viewport.current;
    const content = contents.current;
    if (!element || !content) return;
    const observer = new ResizeObserver(() => {
      if (atBottom.current) element.scrollTop = element.scrollHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, []);
  return (
    <div
      ref={viewport}
      role="region"
      aria-label="Thread history"
      style={{ overflowY: "auto", flex: 1, minHeight: 0 }}
      onScroll={(event) => {
        const element = event.currentTarget;
        atBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
        const first = virtualizer.getVirtualItems()[0];
        const firstEntity = first ? segments[first.index] : undefined;
        if (first && firstEntity) {
          readingAnchor.current = {
            key: `${firstEntity.entityKind}:${firstEntity.entityId}`,
            offset: element.scrollTop - first.start,
          };
        }
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
  const projectedCommands = entities.filter(
    (row): row is ConversationEntity & { state: Extract<ConversationEntity["state"], { outcome: string }> } =>
      row.entityKind === "command" && "outcome" in row.state
  );
  const hasPendingCommands = projectedCommands.length > 0 || commands.local.commands.length > 0;

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
        {hasPendingCommands && (
          <Stack role="region" aria-label="Pending commands" gap="xs">
            {projectedCommands.map((row) => (
              <Paper key={row.entityId} data-command-id={row.entityId} p="xs" withBorder>
                <Text size="xs" c={row.pending ? "dimmed" : row.state.outcome === "failed" ? "red" : undefined}>
                  {row.state.outcome === "pending"
                    ? "Saved · awaiting effect"
                    : `${row.state.operation.replaceAll("_", " ")} · ${row.state.outcome}`}
                  {row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}
                </Text>
                {row.inputRef && <Body threadId={threadId} reference={row.inputRef} follow={false} />}
                <Evidence threadId={threadId} entity={row} />
              </Paper>
            ))}
            {commands.local.commands.map((value) => (
              <Paper key={value.command.commandId} data-command-id={value.command.commandId} p="xs" withBorder>
                <Text size="sm">
                  {value.admission ? "Saved · awaiting effect" : "Saved locally · awaiting admission"}
                </Text>
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
                {commands.errors.get(value.command.commandId) && (
                  <Text c="red">{commands.errors.get(value.command.commandId)}</Text>
                )}
                {!value.admission && <Button onClick={() => void commands.deliver(value)}>Retry</Button>}
              </Paper>
            ))}
          </Stack>
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
    <Stack style={{ flex: 1, minHeight: 0 }}>
      <Group>
        <Button variant="subtle" onClick={onBack}>
          ← Threads
        </Button>
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
  );
}
