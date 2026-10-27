import { ActionIcon, Button, Group, Menu, Select, Stack, Text } from "@mantine/core";
import { IconDotsVertical, IconHistory } from "../icons";
import { intlFormatDistance } from "date-fns";
import { type JSX, useCallback, useEffect, useState } from "react";

import { displayableError } from "../client";
import { harnessLabel, models, modelsForHarness, type ModelOption } from "../model_catalog";
import { type LangfuseSearchSettings, langfuseSandboxTraceUrl } from "../langfuse_search";
import { type TraceSearchSettings, traceSearchUrl } from "../trace_search";
import { type SandboxView } from "../sandboxes/client";
import { type ThreadView } from "./client";
import { decimalBigInt, useThreadSync, type ThreadEntity, type ThreadWindow } from "./thread_sync";
import { historyRows } from "./history_rows";
import { useRequiredThreadsLive } from "./live";
import { useOptionalStreamStatus } from "../stream_status";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import { pendingSentMessage } from "./thread_cards";
import { PendingInputMessages, LocalControlCommands } from "./thread_commands";
import { useThreadCommands } from "./command_delivery";
import { useOpenChronologicalDebug } from "./chronological_debug";
import { installThreadFavicon } from "./thread_favicon";
import { snapshotFresh, threadStatusFromSnapshot } from "./thread_status";
import { isoTime } from "../format_time";
import { TimeAgo } from "../time_ago";
import { autosuspendHint } from "./sandbox_autosuspend";
import { NotificationStatus } from "../notification_status";
import { TopbarActions } from "../topbar";

import { type ThreadTabTitleStatus } from "../tab_metadata";
import { ThreadComposer } from "./composer";
import { VirtualizedHistory } from "./virtualized_history";

/** The app's event-derived hint, not a measured provider send or cache hit. */
function modelActivityAge(at: string | null | undefined, now: number): string {
  if (!at) return "unknown";
  const time = Date.parse(at);
  if (!Number.isFinite(time) || time > now + 60_000) return "unknown";
  if (now - time < 60_000) return "just now";
  return intlFormatDistance(new Date(time), new Date(now), { locale: "en", numeric: "always", style: "narrow" });
}

function canCollapseIdleHint(
  activityAt: string | null | undefined,
  idleSince: string | null | undefined,
  activityAge: string,
  now: number
): boolean {
  if (!activityAt || !idleSince || activityAge === "unknown") return false;
  const activityTime = Date.parse(activityAt);
  const idleTime = Date.parse(idleSince);
  return (
    Number.isFinite(activityTime) &&
    Number.isFinite(idleTime) &&
    Math.abs(activityTime - idleTime) < 60_000 &&
    modelActivityAge(idleSince, now) === activityAge
  );
}

function ModelActivityHint({
  at,
  age,
  idleSince,
  className,
}: {
  at: string | null | undefined;
  age: string;
  idleSince: string | null | undefined;
  className: string;
}): JSX.Element {
  const collapsedIdle = canCollapseIdleHint(at, idleSince, age, Date.now());
  return (
    <Text
      className={className}
      size="xs"
      c="dimmed"
      ta="right"
      aria-label={`Last inferred model activity: ${age}${collapsedIdle ? " · idle" : ""}`}
      title={
        at && age !== "unknown"
          ? `${isoTime(at)} · Inferred from model-originated Thread events, not a measured provider request or cache hit`
          : "No model-originated Thread event observed; provider requests are not measured here"
      }
    >
      Model activity: {age}
      {idleSince &&
        (collapsedIdle ? (
          <time
            dateTime={isoTime(idleSince)}
            title={isoTime(idleSince)}
            aria-label={`Idle since ${isoTime(idleSince)}`}
          >
            {" · Idle"}
          </time>
        ) : (
          <>
            {" · Idle since "}
            <TimeAgo value={idleSince} />
          </>
        ))}
    </Text>
  );
}

function AutosuspendHint({
  hint,
  className,
}: {
  hint: ReturnType<typeof autosuspendHint>;
  className: string;
}): JSX.Element | null {
  if (!hint) return null;
  const title = "Autosuspend waits until all sessions on this Sandbox are idle; new work resets the timer.";
  return (
    <Text
      className={className}
      size="xs"
      c="dimmed"
      ta="right"
      aria-label={hint.text}
      title={hint.deadline ? `${title} Next suspension attempt: ${isoTime(hint.deadline)}` : title}
    >
      {hint.text}
    </Text>
  );
}

interface ConversationBodyProps {
  threadId: string;
  entities: ThreadEntity[];
  thread: ThreadView;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder" | "connection">;
  available: boolean;
  canQueueCommands: boolean;
  canResumeSandbox: boolean;
  composerPlaceholder: string;
  onResumeSandbox: () => void;
  onStatusChange: (status: ThreadTabTitleStatus) => void;
  traceSearch: TraceSearchSettings | null;
  langfuseSearch: LangfuseSearchSettings | null;
  sandbox?: SandboxView;
}

function ConversationBody({
  threadId,
  entities,
  thread,
  history,
  available,
  canQueueCommands,
  canResumeSandbox,
  composerPlaceholder,
  onResumeSandbox,
  onStatusChange,
  traceSearch,
  langfuseSearch,
  sandbox,
}: ConversationBodyProps): JSX.Element {
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const activityAge = modelActivityAge(thread.last_model_activity_at, Math.max(clock, Date.now()));
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const sync = useThreadSync().useThread();
  const commands = useThreadCommands(threadId, entities);
  const onInputEchoLoaded = useCallback(
    (commandId: string) => commands.store.acknowledgeEcho(commandId),
    [commands.store]
  );
  const openDebug = useOpenChronologicalDebug();
  const view = entities.find((row) => row.entityKind === "view_state");
  const controls = view && "controls" in view.state ? view.state.controls : null;
  const operational = view && "controls" in view.state ? view.state.operational : null;
  const syncWritable = sync.window?.caughtUp === true && sync.window.error === null;
  const threadCanAcceptInput =
    syncWritable &&
    !thread.archived &&
    !["SETUP_STATE_FAILED", "SETUP_STATE_INTERRUPTED"].includes(thread.setup_state ?? "") &&
    operational?.status !== "failed";
  const running =
    available && threadCanAcceptInput && operational?.status === "active" && controls?.harness_state === "running";
  // Dispatcher admission does not require a running native harness.
  const canSend = threadCanAcceptInput && (available || canQueueCommands);
  const activeTurn = controls?.active_turn_id ?? null;
  const threadsLive = useRequiredThreadsLive();
  const status = threadStatusFromSnapshot(
    threadsLive.snapshot?.threads.find((candidate) => candidate.id === threadId),
    threadsLive.snapshot?.sandboxes.find((candidate) => candidate.name === thread.sandbox),
    snapshotFresh(threadsLive)
  );
  const currentSandbox = threadsLive.snapshot?.sandboxes.find((candidate) => candidate.name === thread.sandbox);
  const sandboxAutosuspend = autosuspendHint(currentSandbox, snapshotFresh(threadsLive), Math.max(clock, Date.now()));
  useEffect(
    () => onStatusChange({ kind: status.kind, tabLabel: status.tabLabel }),
    [onStatusChange, status.kind, status.tabLabel]
  );
  useEffect(() => installThreadFavicon(status.kind), [status.kind]);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [modelError, setModelError] = useState<string | null>(null);
  const selectedModel = controls?.applied_model ?? thread.model;
  const effortOptions = modelOptions.find((option) => option.model === selectedModel)?.reasoning_efforts ?? [];
  useEffect(() => {
    const harness = thread.harness;
    // Unknown until the Thread's summary folds a runner attachment; no model fits it yet.
    if (harness === null) {
      setModelOptions([]);
      return;
    }
    let active = true;
    void models().then(
      (catalog) => {
        if (active) setModelOptions(modelsForHarness(catalog, harness));
      },
      (reason: unknown) => {
        if (active) setModelError(displayableError(reason));
      }
    );
    return () => {
      active = false;
    };
  }, [thread.harness]);
  const rows = historyRows(
    entities
      .filter(
        (row) =>
          ["item", "confirmed_input", "lifecycle"].includes(row.entityKind) ||
          (row.entityKind === "command" &&
            (pendingSentMessage(row) || ("operation" in row.state && row.state.operation !== "submit_input")))
      )
      .sort((left, right) =>
        decimalBigInt(left.cursor) < decimalBigInt(right.cursor)
          ? -1
          : decimalBigInt(left.cursor) > decimalBigInt(right.cursor)
            ? 1
            : 0
      )
  );
  const localCommands = commands.local.commands;
  const inputEchoes = new Map(commands.local.inputEchoes.map(({ commandId, text }) => [commandId, text]));
  for (const local of localCommands) {
    if (local.command.operation.case === "submitInput") {
      inputEchoes.set(local.command.commandId, local.command.operation.value.text);
    }
  }
  const commandTail = (
    <Stack gap="xs">
      <PendingInputMessages
        commands={localCommands}
        entities={entities}
        inputEchoes={inputEchoes}
        onInputEchoLoaded={onInputEchoLoaded}
        errors={commands.errors}
        store={commands.store}
        deliver={commands.deliver}
      />
      {localCommands.length > 0 && (
        <LocalControlCommands
          commands={localCommands}
          entities={entities}
          store={commands.store}
          errors={commands.errors}
          deliver={commands.deliver}
        />
      )}
      <ModelActivityHint
        at={thread.last_model_activity_at}
        age={activityAge}
        idleSince={activeTurn ? null : thread.idle_since}
        className="agentplane-thread-tail-model-activity"
      />
    </Stack>
  );

  return (
    <RetainedDisclosureProvider>
      <Stack
        style={{ flex: 1, minHeight: 0 }}
        data-projection-cursor={view ? decimalBigInt(view.revisionCursor).toString() : undefined}
      >
        <VirtualizedHistory
          threadId={threadId}
          rows={rows}
          inputEchoes={inputEchoes}
          onInputEchoLoaded={onInputEchoLoaded}
          tail={commandTail}
          running={running}
          activeTurn={activeTurn}
          history={history}
        />
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
        <ThreadComposer
          canSend={canSend}
          running={running}
          activeTurn={activeTurn}
          canResumeSandbox={canResumeSandbox}
          composerPlaceholder={composerPlaceholder}
          onResumeSandbox={onResumeSandbox}
          onSubmit={commands.submitInput}
          onInterrupt={commands.interruptTurn}
          status={<AutosuspendHint hint={sandboxAutosuspend} className="agentplane-composer-autosuspend-mobile" />}
        >
          <Group className="agentplane-composer-settings" gap="xs" wrap="nowrap">
            <Select
              aria-label="Model"
              className="agentplane-composer-model"
              data={modelOptions.map((option) => ({ value: option.model, label: option.display_name }))}
              value={controls?.applied_model ?? null}
              placeholder={
                sync.window?.error || operational?.status === "failed"
                  ? "Model unavailable"
                  : !sync.window?.caughtUp
                    ? "Catching up…"
                    : "Model"
              }
              disabled={!canSend}
              onChange={(model) => model && commands.changeModel(model)}
            />
            {effortOptions.length > 0 && (
              <Select
                aria-label="Reasoning effort"
                className="agentplane-composer-effort"
                data={effortOptions}
                value={controls?.applied_reasoning_effort ?? thread.reasoning_effort ?? null}
                placeholder="Effort"
                disabled={!canSend}
                onChange={(effort) => effort && commands.changeReasoningEffort(effort)}
              />
            )}
          </Group>
          <TopbarActions>
            <Button size="xs" variant="subtle" visibleFrom="sm" onClick={() => setNotificationsOpen(true)}>
              Notifications
            </Button>
            <NotificationStatus
              sandbox={thread.sandbox}
              sessionId={thread.id}
              opened={notificationsOpen}
              onClose={() => setNotificationsOpen(false)}
            />
            <Menu position="bottom-end" withArrow shadow="md">
              <Menu.Target>
                <ActionIcon size="sm" variant="subtle" color="gray" aria-label="More">
                  <IconDotsVertical size={16} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label style={{ overflowWrap: "anywhere" }}>Thread ID: {threadId}</Menu.Label>
                {/* Always here: the topbar drops the same label when the thread's name leaves it no room. */}
                <Menu.Label style={{ overflowWrap: "anywhere" }}>Sandbox: {thread.sandbox}</Menu.Label>
                <Menu.Label>Harness: {harnessLabel(thread.harness)}</Menu.Label>
                <Menu.Divider />
                <Menu.Item hiddenFrom="sm" onClick={() => setNotificationsOpen(true)}>
                  Notifications
                </Menu.Item>
                <Menu.Item leftSection={<IconHistory size={15} />} onClick={() => openDebug()}>
                  Debug history
                </Menu.Item>
                {traceSearch && (
                  <Menu.Item
                    component="a"
                    href={traceSearchUrl(traceSearch, { kind: "session", id: thread.id })}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Find Thread traces in Grafana
                  </Menu.Item>
                )}
                {langfuseSearch && sandbox && (
                  <Menu.Item
                    component="a"
                    href={langfuseSandboxTraceUrl(langfuseSearch, {
                      namespace: sandbox.service_account.namespace,
                      serviceAccount: sandbox.service_account.name,
                    })}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Sandbox LLM traces in Langfuse
                  </Menu.Item>
                )}
                <Menu.Divider />
              </Menu.Dropdown>
            </Menu>
          </TopbarActions>
          <Stack className="agentplane-composer-activity" align="flex-end" gap={0}>
            <AutosuspendHint hint={sandboxAutosuspend} className="agentplane-composer-autosuspend" />
            <ModelActivityHint
              at={thread.last_model_activity_at}
              age={activityAge}
              idleSince={activeTurn ? null : thread.idle_since}
              className="agentplane-composer-model-activity"
            />
          </Stack>
        </ThreadComposer>
      </Stack>
    </RetainedDisclosureProvider>
  );
}

export interface ThreadConversationProps {
  threadId: string;
  thread: ThreadView;
  available: boolean;
  canQueueCommands: boolean;
  canResumeSandbox: boolean;
  composerPlaceholder: string;
  onResumeSandbox: () => void;
  onStatusChange: (status: ThreadTabTitleStatus) => void;
  traceSearch: TraceSearchSettings | null;
  langfuseSearch: LangfuseSearchSettings | null;
  sandbox?: SandboxView;
}

export function ThreadConversation({
  threadId,
  thread,
  available,
  canQueueCommands,
  canResumeSandbox,
  composerPlaceholder,
  onResumeSandbox,
  onStatusChange,
  traceSearch,
  langfuseSearch,
  sandbox,
}: ThreadConversationProps): JSX.Element {
  const { window: shown, error } = useThreadSync().useThread();
  // A stopped window is not following the thread at all, and its alert says so.
  useOptionalStreamStatus("Thread", shown !== null && shown.error === null ? shown.connection : null);
  if (!shown) {
    if (error) return <p role="alert">Thread sync failed: {error}</p>;
    return <p role="status">Loading thread…</p>;
  }
  return (
    <>
      {error && <p role="alert">Thread sync failed: {error}; showing the current window and retrying.</p>}
      {shown.error && (
        <p role="alert">
          Thread synchronization stopped: {shown.error}. Retrying automatically; the rows shown may be out of date.{" "}
          <button onClick={shown.retry}>Retry now</button>
        </p>
      )}
      {!shown.error && !shown.caughtUp && (
        <p role="status" data-thread-catchup="true">
          Catching up thread…
        </p>
      )}
      <ConversationBody
        threadId={threadId}
        entities={shown.caughtUp ? shown.rows : []}
        thread={thread}
        history={shown}
        available={available}
        canQueueCommands={canQueueCommands}
        canResumeSandbox={canResumeSandbox}
        composerPlaceholder={composerPlaceholder}
        onResumeSandbox={onResumeSandbox}
        onStatusChange={onStatusChange}
        traceSearch={traceSearch}
        langfuseSearch={langfuseSearch}
        sandbox={sandbox}
      />
    </>
  );
}
