import { Box, Group, Stack, Text } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import { displayableError } from "../client";
import { type ThreadView, getThread } from "./client";
import { attachedHarness, harnessBehavior } from "../harness_behavior";
import { harnessLabel } from "../model_catalog";
import { resumeSandbox } from "../sandboxes/client";
import { sandboxInventoryFresh, useRequiredSandboxesLive } from "../sandboxes/live";
import { useRequiredThreadsLive } from "./live";
import { ChronologicalDebugProvider } from "./chronological_debug";
import { ThreadTitle } from "./thread_title";
import { ThreadStatusIndicator } from "./thread_status_indicator";
import { snapshotFresh, threadStatusFromSnapshot } from "./thread_status";
import { sandboxReady, sandboxStarting, sandboxSummary, sandboxSuspended, suspensionReason } from "../sandbox_status";
import { useThreadNotices } from "./thread_notices";
import { TopbarTitle } from "../topbar";
import { finishSessionCreate, withinSessionCreate } from "../tracing";
import { useFrontendConfig } from "../frontend_config";
import { appDocumentTitle, CONNECTING_TAB_STATUS, threadDocumentTitle } from "../tab_metadata";
import { useThreadSync } from "./thread_sync";
import { ThreadConversation } from "./conversation";
import { DEFAULT_COMPOSER_PLACEHOLDER } from "./composer";
import "./page.css";
import "./conversation.css";

/** Why the thread's sandbox cannot take commands, as the inventory last reported it; the composer's
 * dot says only "Sandbox unavailable". An absence reads as a deletion only from a current inventory:
 * a stale or dropped stream may not have seen the sandbox since. */
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
  /** Several Threads share the page in a docked workspace, which owns the document title. */
  embedded?: boolean;
}

export function ThreadPage({ threadId, settingsOpen = false, embedded = false }: ThreadPageProps): JSX.Element {
  const sync = useThreadSync();
  const threadsLive = useRequiredThreadsLive();
  const snapshotThread = threadsLive.snapshot?.threads.find((candidate) => candidate.metadata.threadId === threadId);
  const [thread, setThread] = useState<ThreadView | null>(snapshotThread ?? null);
  const notices = useThreadNotices(thread === null ? [] : [threadId]);
  const [tabStatus, setTabStatus] = useState(CONNECTING_TAB_STATUS);
  const environment = useRequiredSandboxesLive();
  const inventoryFresh = sandboxInventoryFresh(environment);
  // The top bar and the composer read the one Sandbox row, so they cannot disagree about it.
  const sandbox = environment.snapshot?.find(
    (candidate) =>
      thread !== null &&
      Boolean(thread.summary.sandbox?.uid) &&
      candidate.target !== undefined &&
      candidate.target.name === thread.summary.sandbox?.name &&
      candidate.target.uid === thread.summary.sandbox?.uid
  );
  const topbarStatus = threadStatusFromSnapshot(snapshotThread, sandbox, snapshotFresh(threadsLive) && inventoryFresh);
  const [error, setError] = useState<string | null>(null);
  const frontendConfig = useFrontendConfig();
  const traceSearch = frontendConfig?.trace_search ?? null;
  const langfuseSearch = frontendConfig?.langfuse_search ?? null;
  const { commandsRequireReadySandbox } = harnessBehavior(attachedHarness(thread?.summary.attached));
  const notice =
    !commandsRequireReadySandbox && sandbox && inventoryFresh && !sandboxReady(sandbox)
      ? "Execution Sandbox is unavailable. Central sampling controls remain available."
      : thread && environment.snapshot && sandboxNotice(sandbox, inventoryFresh);
  const suspended = sandboxSuspended(sandbox);
  const composerPlaceholder = !commandsRequireReadySandbox
    ? DEFAULT_COMPOSER_PLACEHOLDER
    : sandboxComposerPlaceholder(sandbox, inventoryFresh);
  const canQueueCommands =
    commandsRequireReadySandbox &&
    inventoryFresh &&
    sandbox !== undefined &&
    ((suspended && sandbox.configuration?.wakeOnSend) || sandboxStarting(sandbox));
  const canResumeSandbox =
    commandsRequireReadySandbox && inventoryFresh && suspended && sandbox?.configuration?.wakeOnSend === false;
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
  useEffect(() => {
    if (embedded) return;
    document.title = settingsOpen
      ? appDocumentTitle("/", true)
      : threadDocumentTitle(thread?.metadata.name, threadId, tabStatus);
  }, [thread?.metadata.name, tabStatus, threadId, settingsOpen, embedded]);
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
                {thread.summary.sandbox?.name ?? ""} · {harnessLabel(attachedHarness(thread.summary.attached))}
              </Text>
            )}
          </Box>
        </Group>
      </TopbarTitle>
      <Stack style={{ flex: 1, minHeight: 0 }}>
        {error && (
          <Text role="alert" c="red">
            {error}
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
        {thread && (
          <sync.Thread key={threadId} threadId={threadId}>
            <ThreadConversation
              threadId={threadId}
              thread={thread}
              available={
                inventoryFresh && (commandsRequireReadySandbox ? sandboxReady(sandbox) : sandbox !== undefined)
              }
              canQueueCommands={canQueueCommands}
              canResumeSandbox={canResumeSandbox}
              composerPlaceholder={composerPlaceholder}
              onResumeSandbox={() => {
                if (canResumeSandbox && thread && sandbox?.target) {
                  void resumeSandbox(sandbox.target).catch((reason: unknown) => setError(displayableError(reason)));
                }
              }}
              onStatusChange={setTabStatus}
              traceSearch={traceSearch}
              langfuseSearch={langfuseSearch}
              sandbox={sandbox}
            />
          </sync.Thread>
        )}
      </Stack>
    </ChronologicalDebugProvider>
  );
}
