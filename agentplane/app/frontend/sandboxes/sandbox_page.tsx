import { DeliveryState } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";
import { clone, create } from "@bufbuild/protobuf";

import { NotificationStatus } from "../notification_status";
import {
  ActionIcon,
  Alert,
  Anchor,
  Button,
  Group,
  Menu,
  Stack,
  Switch,
  Tabs,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { IconDotsVertical, IconPlus, IconTrash } from "../icons";
import { type JSX, useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import { displayableError } from "../client";
import { archiveThread, findThread, getThread, type ThreadView } from "../threads/client";
import {
  deleteEnvironment,
  getSessionOpening,
  openSession,
  resumeSandbox,
  sessionOpeningRejected,
  sessionOpeningNotVisible,
  suspendEnvironment,
} from "./client";
import { ActionPolicyWatch } from "../actions/policy";
import { EgressLog } from "./egress";
import { useEnvironmentWatch, sandboxInventoryFresh, useRequiredSandboxesLive } from "./live";
import { MarkGlyph } from "../mark_glyph";
import { ConfirmDelete, deletable, DELETE_NEEDS_SUSPENDED, SuspendResume } from "./lifecycle";
import { usePolicyResources } from "../policy/watch";
import { PolicyBindingsSection } from "./policy_bindings";
import { SandboxConsole } from "./sandbox_console";
import { StatusView } from "./sandbox_status_view";
import { hasVmConsole, sandboxReady, sandboxStatusDetail, sandboxSummary, sandboxSuspended } from "../sandbox_status";
import { SANDBOX_STATUS_MARKS } from "../status_mark";
import { snapshotFresh } from "../threads/thread_status";
import { NewThreadForm } from "../threads/new_thread_form";
import { SandboxThreadsTable } from "../threads/sandbox_threads";
import { useThreadsLive } from "../threads/live";
import { beginSessionCreate, finishSessionCreate, withinSessionCreate } from "../tracing";
import { langfuseSandboxTraceUrl } from "../langfuse_search";
import { traceSearchUrl } from "../trace_search";
import { useFrontendConfig } from "../frontend_config";
import { TopbarActions, TopbarTitle } from "../topbar";
import type { SessionSpec } from "../../../proto/session_pb";
import { EnvironmentTargetSchema, type EnvironmentTarget } from "../../../proto/identity_pb";

// Persist only the Session ID, never instructions or setup scripts (which can contain secrets).
function pendingOpenKey(sandbox: string): string {
  return `agentplane:pending-open:${sandbox}`;
}

type PendingOpen = { threadId: string; state?: DeliveryState; reason?: string };

function openingStateLabel(state: DeliveryState | undefined): string {
  switch (state) {
    case DeliveryState.QUEUED_IN_DISPATCHER:
      return "Queued in Dispatcher";
    case DeliveryState.DELIVERING_TO_RUNNER:
      return "Opening on the runner";
    case DeliveryState.RUNNER_ADMITTED:
      return "Accepted by the runner";
    case DeliveryState.FAILED:
      return "Opening failed";
    case DeliveryState.UNKNOWN:
      return "Opening outcome unavailable";
    default:
      return "Waiting for opening status";
  }
}

// The page's tabs, named in the URL (`?tab=`) so a tab can be linked to and survives a reload.
const TABS = ["threads", "egress_log", "policy", "status", "console"] as const;
type Tab = (typeof TABS)[number];
const DEFAULT_TAB: Tab = "threads";

function isTab(value: string | null): value is Tab {
  return TABS.includes(value as Tab);
}

export function SandboxPage({
  name,
  onOpenThread,
  onBack,
}: {
  name: string;
  onOpenThread: (threadId: string) => void;
  onBack: () => void;
}): JSX.Element {
  const frontendConfig = useFrontendConfig();
  const traceSearch = frontendConfig?.trace_search ?? null;
  const langfuseSearch = frontendConfig?.langfuse_search ?? null;
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const [error, setError] = useState<string | null>(null);
  const [creatingThread, setCreatingThread] = useState(false);
  const [pendingOpen, setPendingOpen] = useState<PendingOpen | null>(() => {
    const threadId = sessionStorage.getItem(pendingOpenKey(name));
    return threadId ? { threadId } : null;
  });
  const resolvingOpen = useRef<string | null>(null);
  useEffect(() => {
    const threadId = sessionStorage.getItem(pendingOpenKey(name));
    setPendingOpen(threadId ? { threadId } : null);
  }, [name]);
  function rememberOpen(attempt: PendingOpen | null): void {
    if (attempt) sessionStorage.setItem(pendingOpenKey(name), attempt.threadId);
    else sessionStorage.removeItem(pendingOpenKey(name));
    setPendingOpen(attempt);
  }
  const [launching, setLaunching] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState<EnvironmentTarget | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [threadLimit, setThreadLimit] = useState(200);

  const inventory = useRequiredSandboxesLive();
  const candidate = inventory.snapshot?.find((item) => item.target?.name === name);
  const selected = sandboxInventoryFresh(inventory) ? candidate?.target : undefined;
  const target = selected?.namespace && selected.name && selected.uid ? selected : undefined;
  const sandboxNamespace = target?.namespace;
  const sandboxUID = target?.uid;
  const [lastTarget, setLastTarget] = useState<EnvironmentTarget | null>(null);
  useEffect(() => {
    if (sandboxNamespace && sandboxUID)
      setLastTarget(create(EnvironmentTargetSchema, { namespace: sandboxNamespace, name, uid: sandboxUID }));
  }, [name, sandboxNamespace, sandboxUID]);
  // History remains available after the resource disappears; do not use a prior route's UID.
  const threadUID = sandboxUID ?? (lastTarget?.name === name ? lastTarget.uid : undefined);
  const threadRequest = useMemo(
    () => (threadUID ? { sandboxUID: threadUID, limit: threadLimit, search: null, includeArchived } : null),
    [threadUID, threadLimit, includeArchived]
  );
  const threadLive = useThreadsLive(threadRequest, null);
  // Never combine policy or thread data from an old incarnation with its same-name replacement.
  const observation = useEnvironmentWatch(target);
  const sandbox =
    observation.stream.standing === "current" && observation.stream.connection.phase === "live"
      ? observation.environment
      : null;
  const policy = usePolicyResources(sandbox?.executionAccount?.account, target?.uid);
  const deleted = sandboxInventoryFresh(inventory) && !candidate;
  const vmConsoleAvailable = hasVmConsole(sandbox);
  const tab: Tab =
    isTab(requestedTab) && (requestedTab !== "console" || vmConsoleAvailable) ? requestedTab : DEFAULT_TAB;
  const currentThreads = threadUID && threadLive.snapshot?.sandbox_uid === threadUID ? threadLive.snapshot : null;
  const threadsUnavailable = threadLive.stream.stalled;
  const threads = currentThreads?.threads ?? [];
  const ready = sandboxReady(sandbox);
  const canWakeForThread = sandboxSuspended(sandbox) && sandbox?.configuration?.wakeOnSend === true;
  const threadUnavailableReason = pendingOpen
    ? "An earlier thread open attempt is unconfirmed. Check opening status before creating another thread."
    : !sandbox
      ? deleted
        ? "This Sandbox no longer exists."
        : "Waiting for Sandbox status to load."
      : !ready && !canWakeForThread
        ? sandboxSuspended(sandbox)
          ? "The Sandbox is suspended. Resume it or enable wake on send before creating a thread."
          : `New threads need a ready Sandbox.\n${sandboxStatusDetail(sandbox)}`
        : null;
  useEffect(() => {
    if (requestedTab === "console" && sandbox !== null && !vmConsoleAvailable) {
      setSearchParams({}, { replace: true });
    }
  }, [requestedTab, sandbox, setSearchParams, vmConsoleAvailable]);
  // History's retained Thread projection, keyed by its ID.
  const threadById: Record<string, ThreadView> = Object.fromEntries(
    threads.map((thread) => [thread.metadata.threadId, thread])
  );
  const pendingThreadId = pendingOpen?.threadId;
  const projectedPendingThread = pendingThreadId ? threadById[pendingThreadId] : undefined;
  const visibleThreads = threads.filter((thread) => includeArchived || !thread.metadata.archived);
  const openProjectedPending = useEffectEvent((threadId: string) => {
    void openThread(threadId, threadId);
  });

  // A queued Open is already durable, so the History projection stream is the event that completes
  // it. Match by the client-minted Thread ID; unrelated Threads never resolve the pending attempt.
  useEffect(() => {
    if (!pendingThreadId) {
      resolvingOpen.current = null;
      return;
    }
    if (projectedPendingThread) openProjectedPending(pendingThreadId);
  }, [pendingThreadId, projectedPendingThread]);

  // No re-read after an action: the change reaches the API server, and the watch behind the
  // stream brings the sandbox's new state back on its own.
  async function act(action: "suspend" | "resume"): Promise<void> {
    if (!sandbox?.target) return;
    const failure = await (
      action === "resume" ? resumeSandbox(sandbox.target) : suspendEnvironment(sandbox.target)
    ).then(
      () => null,
      (reason: unknown) => reason
    );
    setError(failure ? displayableError(failure) : null);
  }

  // No refresh after an action: the live stream carries the thread's new archived state back.
  async function threadAct(threadId: string, action: "archive" | "unarchive"): Promise<void> {
    try {
      await archiveThread(threadId, action === "archive");
      setError(null);
    } catch (failure: unknown) {
      setError(displayableError(failure));
    }
  }

  /** Deleting leaves nothing to look at, so a deleted sandbox takes the view back to the list. */
  async function remove(target: EnvironmentTarget): Promise<void> {
    const failure = await deleteEnvironment(target).then(
      () => null,
      (reason: unknown) => reason
    );
    if (!failure) {
      onBack();
      return;
    }
    setError(displayableError(failure));
  }

  async function createThread(spec: SessionSpec, setupScript: string): Promise<void> {
    if (!sandbox || creatingThread || pendingOpen) return;
    const threadId = crypto.randomUUID();
    // Only the Session ID survives reload. In particular, never persist instructions or bootstrap.
    rememberOpen({ threadId });
    beginSessionCreate(threadId, name);
    setLaunching(false);
    setCreatingThread(true);
    setError(null);
    let openingAccepted = false;
    try {
      const opening = await withinSessionCreate(threadId, () =>
        openSession(sandbox.target!, threadId, spec, setupScript)
      );
      if (!opening.sessionId) throw new Error("Open returned without a Session ID");
      openingAccepted = true;
      // The live projection may have resolved this attempt while the Open response was in flight.
      // Do not resurrect its pending record after navigation, or after the user forgot it.
      if (sessionStorage.getItem(pendingOpenKey(name)) !== threadId) {
        finishSessionCreate(threadId, "unknown");
        return;
      }
      rememberOpen({ threadId, state: opening.state, reason: opening.reason });
      if (opening.state === DeliveryState.FAILED) finishSessionCreate(threadId, "failed");
      if (
        opening.state === DeliveryState.RUNNER_ADMITTED &&
        (threadById[threadId] || (await withinSessionCreate(threadId, () => findThread(threadId))))
      ) {
        await openThread(threadId, threadId);
      }
    } catch (reason: unknown) {
      // Outcome may be ambiguous. Do not replay Open or bootstrap, even in this tab.
      if (!openingAccepted) {
        const rejectedStatus = sessionOpeningRejected(reason);
        finishSessionCreate(threadId, rejectedStatus ? "rejected" : "unknown");
      }
      setError(`${displayableError(reason)} Check this attempt before creating another thread.`);
    } finally {
      setCreatingThread(false);
    }
  }

  async function checkPendingOpen(): Promise<void> {
    if (!pendingOpen || creatingThread) return;
    setCreatingThread(true);
    setError(null);
    try {
      const { threadId } = pendingOpen;
      if (threadById[threadId] || (await withinSessionCreate(threadId, () => findThread(threadId)))) {
        await openThread(threadId, threadId);
      } else {
        try {
          const opening = await withinSessionCreate(threadId, () => getSessionOpening(threadId));
          rememberOpen({ threadId, state: opening.state, reason: opening.reason });
          if (opening.state === DeliveryState.FAILED) finishSessionCreate(threadId, "failed");
          setError(`${openingStateLabel(opening.state)}${opening.reason ? `: ${opening.reason}` : "."}`);
        } catch (reason: unknown) {
          if (sessionOpeningNotVisible(reason)) {
            setError(
              "No runner Thread or Dispatcher opening is visible yet. Check later; this attempt is never retried."
            );
          } else {
            setError(displayableError(reason));
          }
        }
      }
    } catch (reason: unknown) {
      setError(displayableError(reason));
    } finally {
      setCreatingThread(false);
    }
  }

  async function openThread(threadId: string, resolvedAttempt?: string): Promise<void> {
    const attempt =
      resolvedAttempt ?? (sessionStorage.getItem(pendingOpenKey(name)) === threadId ? threadId : undefined);
    if (attempt) {
      if (sessionStorage.getItem(pendingOpenKey(name)) !== attempt || resolvingOpen.current === attempt) return;
      resolvingOpen.current = attempt;
    }
    try {
      const thread = attempt
        ? await withinSessionCreate(attempt, async () => threadById[threadId] ?? (await getThread(threadId)))
        : (threadById[threadId] ?? (await getThread(threadId)));
      if (attempt) {
        if (sessionStorage.getItem(pendingOpenKey(name)) !== attempt) {
          resolvingOpen.current = null;
          return;
        }
        rememberOpen(null);
      }
      onOpenThread(thread.metadata.threadId);
    } catch (reason: unknown) {
      if (attempt) resolvingOpen.current = null;
      setError(displayableError(reason));
    }
  }

  return (
    <Stack>
      <TopbarTitle>
        <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
          {sandbox && (
            <Tooltip label={sandboxStatusDetail(sandbox)} multiline style={{ whiteSpace: "pre-line" }} withArrow>
              <span
                role="img"
                tabIndex={0}
                aria-label={`Sandbox status: ${sandboxSummary(sandbox).label}`}
                style={{ display: "inline-flex", flexShrink: 0 }}
              >
                <MarkGlyph mark={SANDBOX_STATUS_MARKS[sandboxSummary(sandbox).kind]} size="small" />
              </span>
            </Tooltip>
          )}
          <Anchor component="button" type="button" size="sm" c="dimmed" visibleFrom="sm" onClick={onBack}>
            Sandboxes ›
          </Anchor>
          <Title
            order={1}
            size="h4"
            style={{
              flex: "1 1 auto",
              minWidth: 0,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {name}
          </Title>
        </Group>
      </TopbarTitle>
      {sandbox && (
        <TopbarActions>
          <Button size="xs" variant="subtle" visibleFrom="sm" onClick={() => setNotificationsOpen(true)}>
            Notifications
          </Button>
          <NotificationStatus
            sandbox={name}
            sessionIds={visibleThreads.map((thread) => thread.metadata.threadId)}
            opened={notificationsOpen}
            onClose={() => setNotificationsOpen(false)}
          />
          <SuspendResume sandbox={sandbox} onAct={(action) => void act(action)} />
          <Menu position="bottom-end">
            <Menu.Target>
              <ActionIcon size="sm" variant="subtle" color="gray" aria-label="More Sandbox actions">
                <IconDotsVertical size={16} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item hiddenFrom="sm" onClick={() => setNotificationsOpen(true)}>
                Notifications
              </Menu.Item>
              {traceSearch && (
                <Menu.Item
                  component="a"
                  href={traceSearchUrl(traceSearch, { kind: "sandbox", id: name })}
                  target="_blank"
                  rel="noreferrer"
                >
                  Find Sandbox traces in Grafana
                </Menu.Item>
              )}
              {langfuseSearch && (
                <Menu.Item
                  component="a"
                  href={langfuseSandboxTraceUrl(langfuseSearch, {
                    namespace: sandbox.executionAccount?.account?.namespace ?? sandbox.target!.namespace,
                    serviceAccount: sandbox.executionAccount?.account?.name ?? sandbox.target!.name,
                  })}
                  target="_blank"
                  rel="noreferrer"
                >
                  Find Sandbox LLM traces in Langfuse
                </Menu.Item>
              )}
              <Menu.Item
                color="red"
                leftSection={<IconTrash size={15} />}
                disabled={!deletable(sandbox)}
                onClick={() => setConfirmingDelete(clone(EnvironmentTargetSchema, sandbox.target!))}
              >
                Delete
                {!deletable(sandbox) && (
                  <Text size="xs" c="dimmed">
                    {DELETE_NEEDS_SUSPENDED}
                  </Text>
                )}
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </TopbarActions>
      )}
      {confirmingDelete && (
        <ConfirmDelete
          name={name}
          onCancel={() => setConfirmingDelete(null)}
          onConfirm={() => {
            void remove(confirmingDelete);
            setConfirmingDelete(null);
          }}
        />
      )}
      {error && (
        <Alert color="red" role="alert">
          {error}
        </Alert>
      )}
      {deleted && (
        <Alert color="red" role="alert">
          There is no sandbox {name} any more.
        </Alert>
      )}
      {sandbox && !ready && (
        <Alert color="gray" role="status">
          Sandbox status: {sandboxSummary(sandbox).label}.{" "}
          {canWakeForThread
            ? "Creating a thread will wake the Sandbox."
            : "New threads need a ready Sandbox or a suspended Sandbox with wake on send enabled."}
        </Alert>
      )}
      {pendingOpen && (
        <Alert color="yellow" role="status" title="Thread opening">
          <Stack gap="xs">
            <Text size="sm">
              Open attempt {pendingOpen.threadId}: {openingStateLabel(pendingOpen.state)}
              {pendingOpen.reason ? ` — ${pendingOpen.reason}` : ""}. Check before creating another thread. An
              unconfirmed attempt is never retried automatically.
            </Text>
            <Group gap="xs">
              <Button disabled={creatingThread} variant="light" onClick={() => void checkPendingOpen()}>
                Check opening status
              </Button>
              <Button
                disabled={creatingThread}
                variant="subtle"
                color="red"
                onClick={() => {
                  rememberOpen(null);
                  setError(null);
                }}
              >
                Forget attempt; it may still open
              </Button>
            </Group>
          </Stack>
        </Alert>
      )}
      <Tabs
        value={tab}
        onChange={(value) => {
          if (!isTab(value)) return;
          setSearchParams(value === DEFAULT_TAB ? {} : { tab: value }, { replace: true });
        }}
      >
        <Tabs.List>
          <Tabs.Tab value="threads">Threads</Tabs.Tab>
          <Tabs.Tab value="egress_log">Egress log</Tabs.Tab>
          <Tabs.Tab value="policy">Policies</Tabs.Tab>
          <Tabs.Tab value="status">Status</Tabs.Tab>
          {vmConsoleAvailable && <Tabs.Tab value="console">Console</Tabs.Tab>}
        </Tabs.List>
        <Tabs.Panel value="egress_log" pt="sm">
          {target && sandbox?.executionAccount?.account && (
            <EgressLog
              key={`${target.namespace}/${target.name}/${target.uid}/${sandbox.executionAccount.account.namespace}/${sandbox.executionAccount.account.name}/${sandbox.executionAccount.uid}`}
              account={sandbox.executionAccount.account}
              sandbox={target.name}
            />
          )}
        </Tabs.Panel>
        <Tabs.Panel value="policy" pt="sm">
          <Stack gap="md">
            {target && (
              <PolicyBindingsSection
                key={`${target.namespace}/${target.name}/${target.uid}`}
                target={target}
                resources={policy.resources}
              />
            )}
            {policy.error && (
              <Alert color="red" role="alert">
                Policy resources unavailable: {policy.error}
              </Alert>
            )}
            {target && !policy.resources && !policy.error && <Text role="status">Waiting for policy resources…</Text>}
            <ActionPolicyWatch subject={sandbox?.executionAccount?.account} fence={target?.uid} />
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="status" pt="sm">
          {sandbox && <StatusView sandbox={sandbox} threads={threads} active={tab === "status"} onError={setError} />}
        </Tabs.Panel>
        {vmConsoleAvailable && (
          <Tabs.Panel value="console" pt="sm">
            {tab === "console" && target && <SandboxConsole target={target} />}
          </Tabs.Panel>
        )}
        <Tabs.Panel value="threads" pt="sm">
          <Stack>
            {threadsUnavailable && (
              <Alert color="red" role="alert">
                History Thread summaries are unavailable ({threadsUnavailable}).
              </Alert>
            )}
            <Group justify="space-between">
              <Button
                leftSection={<IconPlus size={16} />}
                variant={launching ? "light" : "filled"}
                aria-expanded={launching}
                onClick={() => setLaunching((open) => !open)}
              >
                New thread
              </Button>
              <Switch
                size="md"
                label="Show archived"
                checked={includeArchived}
                onChange={(e) => setIncludeArchived(e.currentTarget.checked)}
              />
            </Group>
            {/* Hidden, not unmounted: what was entered, and the model catalog, outlive closing the form. */}
            <div hidden={!launching}>
              <NewThreadForm
                target={sandbox?.target ?? null}
                presetName={sandbox?.configuration?.preset ?? null}
                unavailableReason={threadUnavailableReason}
                creating={creatingThread}
                onCreate={(spec, setupScript) => void createThread(spec, setupScript)}
                onClose={() => setLaunching(false)}
                onError={setError}
              />
            </div>
            {currentThreads !== null && visibleThreads.length === 0 && !threadsUnavailable ? (
              <Text c="dimmed">No threads yet.</Text>
            ) : (
              <SandboxThreadsTable
                threads={visibleThreads}
                sandbox={sandbox}
                traceSearch={traceSearch}
                fresh={snapshotFresh(threadLive)}
                onOpen={(threadId) => void openThread(threadId)}
                onArchive={(threadId, action) => void threadAct(threadId, action)}
              />
            )}
            {currentThreads?.more && (
              <Button variant="subtle" onClick={() => setThreadLimit((limit) => limit + 200)}>
                Load more threads
              </Button>
            )}
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
