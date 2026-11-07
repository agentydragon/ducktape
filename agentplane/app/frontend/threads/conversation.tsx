import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import { ActionIcon, Anchor, Button, Group, Menu, Popover, Select, Stack, Text, UnstyledButton } from "@mantine/core";
import type { LangfuseSearchSettings, TraceSearchSettings } from "../app_config_client";
import type { ThreadView } from "./client";
import { Harness, SetupState } from "../../../proto/session_pb";
import { IconDotsVertical, IconHistory } from "../icons";
import { intlFormatDistance } from "date-fns";
import { type JSX, useCallback, useEffect, useState } from "react";
import { Link } from "react-router";

import { displayableError } from "../client";
import { attachedHarness, harnessBehavior } from "../harness_behavior";
import { harnessLabel, models, modelsForHarness, type ModelOption } from "../model_catalog";
import { langfuseSandboxTraceUrl, langfuseSessionTraceUrl } from "../langfuse_search";
import { traceSearchUrl } from "../trace_search";
import { useThreadSync, type ThreadWindow } from "./thread_sync";
import { ThreadFeedStatus, type ThreadEntity } from "../../../history_service/history_service_pb";
import { historyRows } from "./history_rows";
import { useRequiredThreadsLive } from "./live";
import { sandboxInventoryFresh, useRequiredSandboxesLive } from "../sandboxes/live";
import { useOptionalStreamStatus } from "../stream_status";
import { RetainedDisclosureProvider } from "../../../../util/frontend/transcript/retained_disclosures";
import { pendingSentMessage } from "./thread_cards";
import { PendingInputMessages, LocalControlCommands } from "./thread_commands";
import { useThreadCommands } from "./command_delivery";
import { useOpenChronologicalDebug } from "./chronological_debug";
import { installThreadFavicon } from "./thread_favicon";
import { snapshotFresh, threadStatusFromSnapshot } from "./thread_status";
import { timestampLabel } from "./thread_presentation";
import { isoTime } from "../format_time";
import { TimeAgo } from "../time_ago";
import { AUTOSUSPEND_BLOCKER_LABELS, autosuspendHint } from "./sandbox_autosuspend";
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
  threadId,
}: {
  hint: ReturnType<typeof autosuspendHint>;
  className: string;
  threadId: string;
}): JSX.Element | null {
  const live = useRequiredThreadsLive();
  if (!hint) return null;
  const title = "Autosuspend waits until all sessions on this Sandbox are idle; new work resets the timer.";
  if (hint.blockers?.length) {
    const sibling = hint.blockers.find((blocker) => blocker.sessionId && blocker.sessionId !== threadId);
    const siblingId = sibling?.sessionId;
    const sessionName = (id: string) =>
      live.snapshot?.threads.find((thread) => thread.metadata.threadId === id)?.metadata.name ||
      `Session ${id.slice(0, 8)}`;
    return (
      <Text component="div" className={className} size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
        {siblingId && (
          <>
            {"Sandbox in active use by "}
            <Anchor component={Link} to={`/threads/${encodeURIComponent(siblingId)}`} size="xs">
              {sessionName(siblingId)}
            </Anchor>
            {" · "}
          </>
        )}
        <Popover position="top-end" width={320} withinPortal>
          <Popover.Target>
            <UnstyledButton aria-label="Show autosuspend blockers" title="Show what keeps this Sandbox busy">
              <Text component="span" size="xs" c="dimmed" td="underline">
                {siblingId ? "Details" : hint.text}
              </Text>
            </UnstyledButton>
          </Popover.Target>
          <Popover.Dropdown style={{ overflowWrap: "anywhere" }}>
            <Stack gap="xs">
              <Text size="xs">{title}</Text>
              {hint.blockers.map((blocker) => (
                <Text key={`${blocker.reason}:${blocker.sessionId ?? ""}`} size="xs">
                  {AUTOSUSPEND_BLOCKER_LABELS[blocker.reason] ?? blocker.reason}
                  {blocker.sessionId && (
                    <>
                      {" · "}
                      <Anchor component={Link} to={`/threads/${encodeURIComponent(blocker.sessionId)}`} size="xs">
                        {sessionName(blocker.sessionId)}
                      </Anchor>
                    </>
                  )}
                </Text>
              ))}
            </Stack>
          </Popover.Dropdown>
        </Popover>
      </Text>
    );
  }
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
  sandbox?: EnvironmentDescriptor;
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
  const summary = thread.summary;
  const attachment = summary.attached;
  const spec = attachment?.spec;
  const sandboxName = summary.sandbox?.name ?? "";
  const sandboxUID = summary.sandbox?.uid;
  const lastModelActivityAt = timestampLabel(summary.lastModelActivityAt);
  const idleSince = timestampLabel(summary.idleSince);
  const activityAge = modelActivityAge(lastModelActivityAt, Math.max(clock, Date.now()));
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const sync = useThreadSync().useThread();
  const commands = useThreadCommands(thread, entities);
  const onInputEchoLoaded = useCallback(
    (commandId: string) => commands.store.acknowledgeEcho(commandId),
    [commands.store]
  );
  const openDebug = useOpenChronologicalDebug();
  const view = entities.find((row) => row.entity.case === "viewState");
  const controls = view?.entity.case === "viewState" ? view.entity.value.controls : null;
  const operational = view?.entity.case === "viewState" ? view.entity.value.operational : null;
  const syncWritable = sync.window?.caughtUp === true && sync.window.error === null;
  const threadCanAcceptInput =
    syncWritable &&
    !thread.metadata.archived &&
    attachment?.setupState !== SetupState.FAILED &&
    attachment?.setupState !== SetupState.INTERRUPTED &&
    operational?.status !== ThreadFeedStatus.FAILED;
  const running =
    available &&
    threadCanAcceptInput &&
    operational?.status === ThreadFeedStatus.ACTIVE &&
    controls?.harnessState === "running";
  // Dispatcher admission does not require a running native harness.
  const canSend = threadCanAcceptInput && (available || canQueueCommands);
  const activeTurn = controls?.activeTurnId ?? null;
  const threadsLive = useRequiredThreadsLive();
  const sandboxesLive = useRequiredSandboxesLive();
  const inventoryFresh = sandboxInventoryFresh(sandboxesLive);
  const currentSandbox = sandboxesLive.snapshot?.find(
    (candidate) => Boolean(sandboxUID) && candidate.target?.name === sandboxName && candidate.target.uid === sandboxUID
  );
  const status = threadStatusFromSnapshot(
    threadsLive.snapshot?.threads.find((candidate) => candidate.metadata.threadId === threadId),
    currentSandbox,
    snapshotFresh(threadsLive) && inventoryFresh
  );
  const harness = attachedHarness(attachment);
  const { llmTraceScope } = harnessBehavior(harness);
  const sandboxAutosuspend = autosuspendHint(currentSandbox, inventoryFresh, Math.max(clock, Date.now()));
  useEffect(
    () => onStatusChange({ kind: status.kind, tabLabel: status.tabLabel }),
    [onStatusChange, status.kind, status.tabLabel]
  );
  useEffect(() => installThreadFavicon(status.kind), [status.kind]);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [modelError, setModelError] = useState<string | null>(null);
  const selectedModel = controls?.appliedModel ?? spec?.model;
  const effortOptions = modelOptions.find((option) => option.model === selectedModel)?.reasoning_efforts ?? [];
  useEffect(() => {
    // Unknown until the Thread's summary folds a runner attachment; no model fits it yet.
    if (harness === undefined || harness === Harness.UNSPECIFIED) {
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
  }, [harness]);
  const rows = historyRows(
    entities
      .filter(
        (row) =>
          row.entity.case === "item" ||
          row.entity.case === "confirmedInput" ||
          row.entity.case === "lifecycle" ||
          (row.entity.case === "command" && (pendingSentMessage(row) || row.entity.value.operation !== "submit_input"))
      )
      .sort((left, right) => (left.cursor < right.cursor ? -1 : left.cursor > right.cursor ? 1 : 0))
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
        at={lastModelActivityAt}
        age={activityAge}
        idleSince={activeTurn ? null : idleSince}
        className="agentplane-thread-tail-model-activity"
      />
    </Stack>
  );

  return (
    <RetainedDisclosureProvider>
      <Stack style={{ flex: 1, minHeight: 0 }} data-projection-cursor={view?.revisionCursor.toString()}>
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
        {operational?.feedError && (
          <Text role="alert" c="red">
            {operational.feedError.cursor === undefined
              ? `Projection failed: ${operational.feedError.message}. Showing verified history through event ${operational.lastVerifiedCursor}.`
              : `Rejected event ${operational.feedError.cursor}: ${operational.feedError.message}. Showing verified history through event ${operational.lastVerifiedCursor}.`}
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
          status={
            <AutosuspendHint
              threadId={threadId}
              hint={sandboxAutosuspend}
              className="agentplane-composer-autosuspend-mobile"
            />
          }
        >
          <Group className="agentplane-composer-settings" gap="xs" wrap="nowrap">
            <Select
              aria-label="Model"
              className="agentplane-composer-model"
              data={modelOptions.map((option) => ({ value: option.model, label: option.display_name }))}
              value={controls?.appliedModel ?? null}
              placeholder={
                sync.window?.error || operational?.status === ThreadFeedStatus.FAILED
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
                value={controls?.appliedReasoningEffort ?? spec?.reasoningEffort ?? null}
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
              sandbox={sandboxName}
              sessionIds={[thread.metadata.threadId]}
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
                <Menu.Label style={{ overflowWrap: "anywhere" }}>Sandbox: {sandboxName}</Menu.Label>
                <Menu.Label>Harness: {harnessLabel(harness)}</Menu.Label>
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
                    href={traceSearchUrl(traceSearch, { kind: "session", id: thread.metadata.threadId })}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Find Thread traces in Grafana
                  </Menu.Item>
                )}
                {langfuseSearch && (sandbox || llmTraceScope === "session") && (
                  <Menu.Item
                    component="a"
                    href={
                      llmTraceScope === "session"
                        ? langfuseSessionTraceUrl(langfuseSearch, thread.metadata.threadId)
                        : langfuseSandboxTraceUrl(langfuseSearch, {
                            namespace: sandbox!.executionAccount?.account?.namespace ?? "",
                            serviceAccount: sandbox!.executionAccount?.account?.name ?? "",
                          })
                    }
                    target="_blank"
                    rel="noreferrer"
                  >
                    {llmTraceScope === "session" ? "Session" : "Sandbox"} LLM traces in Langfuse
                  </Menu.Item>
                )}
                <Menu.Divider />
              </Menu.Dropdown>
            </Menu>
          </TopbarActions>
          <Stack className="agentplane-composer-activity" align="flex-end" gap={0}>
            <AutosuspendHint
              threadId={threadId}
              hint={sandboxAutosuspend}
              className="agentplane-composer-autosuspend"
            />
            <ModelActivityHint
              at={lastModelActivityAt}
              age={activityAge}
              idleSince={activeTurn ? null : idleSince}
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
  sandbox?: EnvironmentDescriptor;
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
