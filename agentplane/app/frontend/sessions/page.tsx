import { Box, Group, Stack, Text } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import { displayableError } from "../transport/client";
import { type ThreadView, getThread, sessionEnvironment } from "./client";
import { Harness } from "../../../proto/session_pb";
import { SessionStatus_Materialization } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { attachedHarness, harnessBehavior } from "./harness_behavior";
import { harnessLabel } from "./model_catalog";
import { resumeSandbox } from "../sandboxes/client";
import { sandboxInventoryFresh, useRequiredSandboxesLive } from "../sandboxes/live";
import { useRequiredThreadsLive } from "./live";
import { ChronologicalDebugProvider } from "./chronological_debug";
import { ThreadTitle } from "./thread_title";
import { ThreadStatusIndicator } from "./thread_status_indicator";
import { installThreadFavicon } from "./thread_favicon";
import { sessionActivityFresh, sessionMaterializationLabel, threadStatusFromSnapshot } from "./thread_status";
import {
  sandboxReady,
  sandboxStarting,
  sandboxSummary,
  sandboxSuspended,
  suspensionReason,
} from "../sandboxes/sandbox_status";
import { useThreadNotices } from "../notifications/thread_notices";
import { TopbarTitle } from "../shell/topbar";
import { finishSessionCreate, withinSessionCreate } from "../observability/tracing";
import { useFrontendConfig } from "../app_config/frontend_config";
import { appDocumentTitle, threadDocumentTitle } from "../shell/tab_metadata";
import { useThreadSync } from "./thread_sync";
import { CommandDeliveryHistory, DeliveryHistoryFallback } from "./command_delivery_history";
import { ThreadConversation } from "./conversation";
import { DispatchWorksetPanel, useDispatchWorkset } from "./dispatch_workset_panel";
import { DEFAULT_COMPOSER_PLACEHOLDER } from "./composer";
import "./page.css";
import "./conversation.css";

/** The Environment inventory supplies command availability and wake policy independently of Session activity. */
function suspendedSandboxLabel(sandbox: EnvironmentDescriptor): string {
  const reason = suspensionReason(sandbox);
  return reason === null ? "Sandbox is suspended." : `Sandbox is suspended (${reason}).`;
}

function sandboxComposerPlaceholder(sandbox: EnvironmentDescriptor | undefined, inventoryFresh: boolean): string {
  if (!sandbox || !inventoryFresh || !sandboxSuspended(sandbox)) return DEFAULT_COMPOSER_PLACEHOLDER;
  return sandbox.configuration?.wakeOnSend
    ? `${suspendedSandboxLabel(sandbox)} Wake on message is on; submitted commands will queue while it wakes. ${DEFAULT_COMPOSER_PLACEHOLDER}`
    : `${suspendedSandboxLabel(sandbox)} Wake on message is off; submitted commands stay queued until you resume it manually.`;
}

function sandboxNotice(sandbox: EnvironmentDescriptor | undefined, inventoryFresh: boolean): string | null {
  if (sandbox === undefined) {
    return inventoryFresh
      ? "Sandbox no longer exists. Showing archived Thread history; controls are disabled."
      : "Sandbox absent from last inventory snapshot. Current availability unknown; controls are disabled.";
  }
  if (sandboxReady(sandbox)) return null;
  if (sandboxSuspended(sandbox) && inventoryFresh) return null;
  const summary = sandboxSummary(sandbox);
  if (inventoryFresh && sandboxStarting(sandbox)) {
    return "Sandbox is starting. Submitted commands will queue until it is ready.";
  }
  return `Last observed Sandbox and Pod: ${summary.label}. Showing retained Thread history; controls are disabled.`;
}

export interface ThreadPageProps {
  threadId: string;
  settingsOpen?: boolean;
}

export function ThreadPage({ threadId, settingsOpen = false }: ThreadPageProps): JSX.Element {
  const sync = useThreadSync();
  const [historySession, setHistorySession] = useState<string | null>(null);
  const openDeliveryHistory = () => setHistorySession(threadId);
  const threadsLive = useRequiredThreadsLive();
  const snapshotThread = threadsLive.snapshot?.threads.find((candidate) => candidate.metadata.threadId === threadId);
  const [thread, setThread] = useState<ThreadView | null>(snapshotThread ?? null);
  const dispatchWorkset = useDispatchWorkset(thread?.metadata.threadId === threadId ? threadId : null);
  const notices = useThreadNotices(thread === null ? [] : [threadId]);
  const environment = useRequiredSandboxesLive();
  const inventoryFresh = sandboxInventoryFresh(environment);
  const target = sessionEnvironment(thread?.session) ?? thread?.summary.sandbox;
  // The top bar and the composer read the one Sandbox row, so they cannot disagree about it.
  const sandbox = environment.snapshot?.find(
    (candidate) =>
      thread !== null &&
      Boolean(target?.uid) &&
      candidate.target !== undefined &&
      candidate.target.name === target?.name &&
      candidate.target.uid === target?.uid
  );
  const topbarStatus = threadStatusFromSnapshot(snapshotThread, sessionActivityFresh(threadsLive));
  const [error, setError] = useState<string | null>(null);
  const frontendConfig = useFrontendConfig();
  const traceSearch = frontendConfig?.traceSearch ?? null;
  const langfuseSearch = frontendConfig?.langfuseSearch ?? null;
  const placement = thread?.session?.placement?.kind;
  const harness =
    attachedHarness(thread?.summary.attached) ??
    (placement?.case === "centralSamplerV1"
      ? Harness.CENTRAL_SAMPLER
      : placement?.case === "environmentHosted"
        ? placement.value.harness
        : undefined);
  const { commandsRequireReadySandbox } = harnessBehavior(harness);
  const materialization = thread?.session?.status?.materialization;
  const materializationUnconfirmed =
    materialization === SessionStatus_Materialization.SESSION_MATERIALIZATION_FAILED ||
    materialization === SessionStatus_Materialization.SESSION_MATERIALIZATION_UNKNOWN;
  const showConversation = !materializationUnconfirmed || Boolean(thread?.summary.projectionEpoch);
  const notice =
    !materializationUnconfirmed && !commandsRequireReadySandbox && sandbox && inventoryFresh && !sandboxReady(sandbox)
      ? "Execution Sandbox is unavailable. Central sampling controls remain available."
      : thread &&
        environment.snapshot &&
        (target?.uid
          ? sandboxNotice(sandbox, inventoryFresh)
          : "Sandbox identity unavailable. Current availability unknown; controls are disabled.");
  const suspended = sandboxSuspended(sandbox);
  const composerPlaceholder = !commandsRequireReadySandbox
    ? DEFAULT_COMPOSER_PLACEHOLDER
    : sandboxComposerPlaceholder(sandbox, inventoryFresh);
  const canQueueCommands =
    !materializationUnconfirmed &&
    commandsRequireReadySandbox &&
    inventoryFresh &&
    sandbox !== undefined &&
    ((suspended && sandbox.configuration?.wakeOnSend) || sandboxStarting(sandbox));
  const canResumeSandbox =
    !materializationUnconfirmed &&
    commandsRequireReadySandbox &&
    inventoryFresh &&
    suspended &&
    sandbox?.configuration?.wakeOnSend === false;
  useEffect(() => {
    let current = true;
    setError(null);
    if (snapshotThread) {
      setThread(snapshotThread);
    } else {
      setThread(null);
      void withinSessionCreate(threadId, () => getThread(threadId)).then(
        (view) => {
          if (current) setThread(view);
        },
        (reason: unknown) => {
          if (current) setError(displayableError(reason));
        }
      );
    }
    return () => {
      current = false;
    };
  }, [snapshotThread, threadId]);
  useEffect(() => {
    if (thread !== null) finishSessionCreate(threadId);
  }, [thread, threadId]);
  const documentTitle = settingsOpen
    ? appDocumentTitle("/", true)
    : threadDocumentTitle(thread?.metadata.name, threadId, topbarStatus);
  useEffect(() => {
    document.title = documentTitle;
  }, [documentTitle]);
  useEffect(() => installThreadFavicon(topbarStatus.kind), [topbarStatus.kind]);
  return (
    <ChronologicalDebugProvider key={threadId} threadId={threadId}>
      <TopbarTitle>
        <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
          {thread && (
            <ThreadStatusIndicator
              kind={topbarStatus.kind}
              label={topbarStatus.label}
              inbox={notices.bySession.get(thread.metadata.threadId)}
              live={notices.live.has(thread.metadata.threadId)}
            />
          )}
          <Box className="agentplane-thread-heading">
            <ThreadTitle
              className="agentplane-thread-heading-name"
              threadId={threadId}
              thread={thread}
              onRenamed={(metadata) =>
                setThread((current) =>
                  current && current.metadata.threadId === metadata.threadId ? { ...current, metadata } : current
                )
              }
              onError={setError}
            />
            {thread && (
              <Text size="xs" c="dimmed" className="agentplane-thread-heading-sandbox">
                {target?.name ?? ""} · {harnessLabel(harness)}
              </Text>
            )}
          </Box>
        </Group>
      </TopbarTitle>
      <CommandDeliveryHistory
        sessionId={threadId}
        issues={dispatchWorkset.snapshot?.recentIssues ?? []}
        opened={historySession === threadId}
        onClose={() => setHistorySession(null)}
      />
      {(!thread || !showConversation) && <DeliveryHistoryFallback onOpen={openDeliveryHistory} />}
      <Stack style={{ flex: 1, minHeight: 0 }}>
        {error && (
          <Text role="alert" c="red">
            {error}
          </Text>
        )}
        {thread && materializationUnconfirmed && (
          <Text role="alert" c="red">
            {sessionMaterializationLabel(thread)}
          </Text>
        )}
        {thread?.metadata.archived && (
          <Text role="status" c="dimmed">
            Thread archived. Showing retained thread history; controls are disabled.
          </Text>
        )}
        {notice && (
          <Text role="status" c="dimmed">
            {notice}
          </Text>
        )}
        {thread && showConversation && (
          <sync.Thread key={threadId} threadId={threadId}>
            <ThreadConversation
              threadId={threadId}
              thread={thread}
              available={
                !materializationUnconfirmed &&
                inventoryFresh &&
                (commandsRequireReadySandbox ? sandboxReady(sandbox) : sandbox !== undefined)
              }
              canQueueCommands={canQueueCommands}
              canResumeSandbox={canResumeSandbox}
              composerPlaceholder={composerPlaceholder}
              onOpenDeliveryHistory={openDeliveryHistory}
              onResumeSandbox={() => {
                if (canResumeSandbox && thread && sandbox?.target) {
                  void resumeSandbox(sandbox.target).catch((reason: unknown) => setError(displayableError(reason)));
                }
              }}
              traceSearch={traceSearch}
              langfuseSearch={langfuseSearch}
              sandbox={sandbox}
              dispatchWorkset={dispatchWorkset}
            />
          </sync.Thread>
        )}
        {thread && materializationUnconfirmed && !thread.summary.projectionEpoch && (
          <DispatchWorksetPanel
            key={threadId}
            sessionId={threadId}
            view={dispatchWorkset}
            onOpenDeliveryHistory={openDeliveryHistory}
          />
        )}
      </Stack>
    </ChronologicalDebugProvider>
  );
}
