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
import { Markdown } from "./markdown";

const EMPTY_LOCAL: LocalCommandSnapshot = { commands: [], error: null };

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
  const [open, setOpen] = useState(false);
  return (
    <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{label}</summary>
      {open && <Body {...body} />}
    </details>
  );
}

function EvidenceFrames({
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
  const request = useRef<AbortController | null>(null);
  const scope = {
    sourceId: entity.sourceId,
    projectionEpoch: entity.projectionEpoch,
    entityKind: entity.entityKind,
    entityId: entity.entityId,
  };
  useEffect(() => () => request.current?.abort(), []);
  const load = (after = "0"): void => {
    if (loading) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    void conversationFrames(threadId, scope, observationCursor, after, controller.signal)
      .then(
        (value) => {
          if (!controller.signal.aborted) setPage(value);
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
      {!page && (
        <Button loading={loading} onClick={() => load()}>
          Load raw frames
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

function EvidencePageView({ threadId, entity }: { threadId: string; entity: ConversationEntity }): JSX.Element {
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
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
          if (!controller.signal.aborted) setPage(value);
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
        <Paper p="sm" withBorder maw="80%">
          <Body threadId={threadId} reference={entity.inputRef} follow={false} />
          <Evidence threadId={threadId} entity={entity} />
        </Paper>
      </Group>
    );
  }
  if (entity.entityKind === "lifecycle") {
    return (
      <Stack gap="xs" data-conversation-anchor={entity.cursor.toString()}>
        <Text size="xs" c="dimmed">
          {"observation" in entity.state ? entity.state.observation : "lifecycle"}
        </Text>
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
        {streaming && <Text size="xs">Streaming</Text>}
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
    () => new Set(entities.filter((row) => row.entityKind === "command").map((row) => row.entityId)),
    [entities]
  );
  const localCommandKey = local.commands
    .slice(0, 128)
    .map((value) => value.command.commandId)
    .join("\u0000");
  useEffect(() => store.observeCommandIds(commandIds), [commandIds, store]);
  const scope = entities.find((row) => row.entityKind === "view_state");
  const sourceId = scope?.sourceId;
  const projectionEpoch = scope?.projectionEpoch;
  useEffect(() => {
    if (!sourceId || !projectionEpoch || !localCommandKey) return;
    const controller = new AbortController();
    void reconcileCommands(threadId, sourceId, projectionEpoch, localCommandKey.split("\u0000")).then(
      (result) => {
        if (!controller.signal.aborted) {
          store.observeCommandIds(
            new Set(result.commands.filter((value) => value.outcome !== null).map((value) => value.command_id))
          );
        }
      },
      (reason: unknown) => {
        if (!controller.signal.aborted)
          setErrors((previous) => new Map(previous).set("reconciliation", displayableError(reason)));
      }
    );
    return () => controller.abort();
  }, [localCommandKey, projectionEpoch, sourceId, store, threadId]);

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
  const atBottom = useRef(true);
  const previousCount = useRef(segments.length);
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
    if (element && atBottom.current && segments.length > previousCount.current)
      element.scrollTop = element.scrollHeight;
    if (element && previousCount.current === 0 && segments.length > 0 && readingAnchor.current) {
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
  }, [segments, virtualizer]);
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
        const oldest = segments[0]?.cursor.toString();
        if (element.scrollTop < 80 && oldest && requestedBefore.current !== oldest) {
          requestedBefore.current = oldest;
          onLoadOlder(oldest);
        }
      }}
    >
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
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
}: {
  threadId: string;
  entities: ConversationEntity[];
  thread: ThreadView;
  onLoadOlder: (cursor: string) => void;
}): JSX.Element {
  const [draft, setDraft] = useState("");
  const commands = useProjectedCommands(threadId, entities);
  const view = entities.find((row) => row.entityKind === "view_state");
  const controls = view && "controls" in view.state ? view.state.controls : null;
  const running = !thread.archived && controls?.harness_state === "running";
  const activeTurn = controls?.active_turn_id ?? null;
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  useEffect(() => {
    let active = true;
    void models().then((catalog) => {
      if (active) setModelOptions(catalog[thread.harness] ?? []);
    });
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

  function submit(): void {
    if (!draft.trim() || !running) return;
    const value = create(CommandSchema, {
      commandId: crypto.randomUUID(),
      operation: { case: "submitInput", value: { text: draft } },
    });
    if (commands.submit(value)) setDraft("");
  }

  return (
    <Stack style={{ flex: 1, minHeight: 0 }}>
      <Button
        variant="subtle"
        disabled={!segments.length}
        onClick={() => segments[0] && onLoadOlder(segments[0].cursor.toString())}
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
      {entities
        .filter((row) => row.entityKind === "command")
        .map((row) => {
          if (!("outcome" in row.state)) return null;
          return (
            <Stack key={row.entityId} gap="xs">
              <Text size="xs" c={row.pending ? "dimmed" : row.state.outcome === "failed" ? "red" : undefined}>
                {row.state.operation} · {row.state.outcome}
                {row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}
              </Text>
              <Evidence threadId={threadId} entity={row} />
            </Stack>
          );
        })}
      {commands.local.commands.map((value) => (
        <Paper key={value.command.commandId} p="xs" withBorder>
          <Text size="sm">Pending locally saved command</Text>
          {commands.errors.get(value.command.commandId) && (
            <Text c="red">{commands.errors.get(value.command.commandId)}</Text>
          )}
          {!value.admission && <Button onClick={() => void commands.deliver(value)}>Retry</Button>}
        </Paper>
      ))}
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
  );
}

export function ProjectedSession({ threadId, onBack }: { threadId: string; onBack: () => void }): JSX.Element {
  const [thread, setThread] = useState<ThreadView | null>(null);
  const [before, setBefore] = useState<string | undefined>();
  const [name, setName] = useState("");
  useEffect(() => {
    void getThread(threadId).then((value) => {
      setThread(value);
      setName(value.name ?? "");
    });
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
          onBlur={() => void renameThread(threadId, name.trim() || null).then(setThread)}
        />
      </Group>
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
      {thread && (
        <ConversationCollection key={threadId} threadId={threadId} beforeCursor={before}>
          {(rows) => (
            <ProjectedSessionBody threadId={threadId} entities={rows} thread={thread} onLoadOlder={setBefore} />
          )}
        </ConversationCollection>
      )}
    </Stack>
  );
}
