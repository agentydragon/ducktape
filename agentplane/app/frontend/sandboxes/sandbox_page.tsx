import { SessionStatus_Materialization } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";
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
  getSession,
  createSession,
  resumeSandbox,
  sessionCreateRejected,
  sessionNotVisible,
  suspendEnvironment,
} from "./client";
import { ActionPolicyWatch } from "../actions/policy";
import { EgressLog } from "./egress";
import { EnvironmentIdentity } from "./identity";
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
function pendingCreateKey(sandbox: string): string {
  return `agentplane:pending-create:${sandbox}`;
}

type PendingCreate = { threadId: string; state?: SessionStatus_Materialization; reason?: string };

function sessionStateLabel(state: SessionStatus_Materialization | undefined): string {
  switch (state) {
    case SessionStatus_Materialization.SESSION_MATERIALIZATION_WAITING:
      return "Waiting for the placement owner";
    case SessionStatus_Materialization.SESSION_MATERIALIZATION_IN_PROGRESS:
      return "Creating the Session";
    case SessionStatus_Materialization.SESSION_MATERIALIZATION_ACCEPTED:
      return "Accepted by the placement owner";
    case SessionStatus_Materialization.SESSION_MATERIALIZATION_FAILED:
      return "Session creation failed";
    case SessionStatus_Materialization.SESSION_MATERIALIZATION_UNKNOWN:
      return "Session creation outcome unavailable";
    default:
      return "Waiting for Session status";
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
  const traceSearch = frontendConfig?.traceSearch ?? null;
  const langfuseSearch = frontendConfig?.langfuseSearch ?? null;
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const [error, setError] = useState<string | null>(null);
  const [creatingThread, setCreatingThread] = useState(false);
  const [pendingCreate, setPendingCreate] = useState<PendingCreate | null>(() => {
    const threadId = sessionStorage.getItem(pendingCreateKey(name));
    return threadId ? { threadId } : null;
  });
  const resolvingCreateAttempt = useRef<string | null>(null);
  useEffect(() => {
    const threadId = sessionStorage.getItem(pendingCreateKey(name));
    setPendingCreate(threadId ? { threadId } : null);
  }, [name]);
  function rememberCreateAttempt(attempt: PendingCreate | null): void {
    if (attempt) sessionStorage.setItem(pendingCreateKey(name), attempt.threadId);
    else sessionStorage.removeItem(pendingCreateKey(name));
    setPendingCreate(attempt);
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
  const account = sandbox?.executionAccount?.account;
  const policy = usePolicyResources(account ? { case: "subject", value: account } : undefined, target?.uid);
  const deleted = sandboxInventoryFresh(inventory) && !candidate;
  const vmConsoleAvailable = hasVmConsole(sandbox);
  const tab: Tab =
    isTab(requestedTab) && (requestedTab !== "console" || vmConsoleAvailable) ? requestedTab : DEFAULT_TAB;
  const currentThreads = threadUID && threadLive.snapshot?.sandbox_uid === threadUID ? threadLive.snapshot : null;
  const threadsUnavailable = threadLive.stream.stalled;
  const threads = currentThreads?.threads ?? [];
  const ready = sandboxReady(sandbox);
  const canWakeForThread = sandboxSuspended(sandbox) && sandbox?.configuration?.wakeOnSend === true;
  const threadUnavailableReason = pendingCreate
    ? "An earlier Session creation is unconfirmed. Check its status before creating another thread."
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
  const pendingThreadId = pendingCreate?.threadId;
  const projectedPendingThread = pendingThreadId ? threadById[pendingThreadId] : undefined;
  const visibleThreads = threads.filter((thread) => includeArchived || !thread.metadata.archived);
  const openProjectedPending = useEffectEvent((threadId: string) => {
    void openThread(threadId, threadId);
  });

  // A Session can be accepted before History projects its Thread, so the History stream completes
  // it. Match by the client-minted Thread ID; unrelated Threads never resolve the pending attempt.
  useEffect(() => {
    if (!pendingThreadId) {
      resolvingCreateAttempt.current = null;
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
    if (!sandbox || creatingThread || pendingCreate) return;
    const threadId = crypto.randomUUID();
    // Only the Session ID survives reload. In particular, never persist instructions or bootstrap.
    rememberCreateAttempt({ threadId });
    beginSessionCreate(threadId, name);
    setLaunching(false);
    setCreatingThread(true);
    setError(null);
    let createAccepted = false;
    try {
      const session = await withinSessionCreate(threadId, () =>
        createSession(sandbox.target!, threadId, spec, setupScript)
      );
      if (!session.sessionId) throw new Error("Create returned without a Session ID");
      createAccepted = true;
      // The live projection may have resolved this attempt while the Create response was in flight.
      // Do not resurrect its pending record after navigation, or after the user forgot it.
      if (sessionStorage.getItem(pendingCreateKey(name)) !== threadId) {
        finishSessionCreate(threadId, "unknown");
        return;
      }
      rememberCreateAttempt({ threadId, state: session.status?.materialization, reason: session.status?.reason });
      if (session.status?.materialization === SessionStatus_Materialization.SESSION_MATERIALIZATION_FAILED)
        finishSessionCreate(threadId, "failed");
      if (
        session.status?.materialization === SessionStatus_Materialization.SESSION_MATERIALIZATION_ACCEPTED &&
        (threadById[threadId] || (await withinSessionCreate(threadId, () => findThread(threadId))))
      ) {
        await openThread(threadId, threadId);
      }
    } catch (reason: unknown) {
      // Outcome may be ambiguous. Do not replay Create or bootstrap, even in this tab.
      if (!createAccepted) {
        const rejectedStatus = sessionCreateRejected(reason);
        finishSessionCreate(threadId, rejectedStatus ? "rejected" : "unknown");
      }
      setError(`${displayableError(reason)} Check this attempt before creating another thread.`);
    } finally {
      setCreatingThread(false);
    }
  }

  async function checkPendingCreate(): Promise<void> {
    if (!pendingCreate || creatingThread) return;
    setCreatingThread(true);
    setError(null);
    try {
      const { threadId } = pendingCreate;
      if (threadById[threadId] || (await withinSessionCreate(threadId, () => findThread(threadId)))) {
        await openThread(threadId, threadId);
      } else {
        try {
          const session = await withinSessionCreate(threadId, () => getSession(threadId));
          rememberCreateAttempt({ threadId, state: session.status?.materialization, reason: session.status?.reason });
          if (session.status?.materialization === SessionStatus_Materialization.SESSION_MATERIALIZATION_FAILED)
            finishSessionCreate(threadId, "failed");
          const state = session.status?.materialization;
          const reason = session.status?.reason;
          setError(`${sessionStateLabel(state)}${reason ? `: ${reason}` : "."}`);
        } catch (reason: unknown) {
          if (sessionNotVisible(reason)) {
            setError("No runner Thread or Session is visible yet. Check later; this attempt is never retried.");
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
      resolvedAttempt ?? (sessionStorage.getItem(pendingCreateKey(name)) === threadId ? threadId : undefined);
    if (attempt) {
      if (sessionStorage.getItem(pendingCreateKey(name)) !== attempt || resolvingCreateAttempt.current === attempt)
        return;
      resolvingCreateAttempt.current = attempt;
    }
    try {
      const thread = attempt
        ? await withinSessionCreate(attempt, async () => threadById[threadId] ?? (await getThread(threadId)))
        : (threadById[threadId] ?? (await getThread(threadId)));
      if (attempt) {
        if (sessionStorage.getItem(pendingCreateKey(name)) !== attempt) {
          resolvingCreateAttempt.current = null;
          return;
        }
        rememberCreateAttempt(null);
      }
      onOpenThread(thread.metadata.threadId);
    } catch (reason: unknown) {
      if (attempt) resolvingCreateAttempt.current = null;
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
      {pendingCreate && (
        <Alert color="yellow" role="status" title="Session creation">
          <Stack gap="xs">
            <Text size="sm">
              Session {pendingCreate.threadId}: {sessionStateLabel(pendingCreate.state)}
              {pendingCreate.reason ? ` — ${pendingCreate.reason}` : ""}. Check before creating another thread. An
              unconfirmed attempt is never retried automatically.
            </Text>
            <Group gap="xs">
              <Button disabled={creatingThread} variant="light" onClick={() => void checkPendingCreate()}>
                Check Session status
              </Button>
              <Button
                disabled={creatingThread}
                variant="subtle"
                color="red"
                onClick={() => {
                  rememberCreateAttempt(null);
                  setError(null);
                }}
              >
                Forget attempt; Session creation may still complete
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
          {sandbox && tab === "status" && (
            <Stack>
              <EnvironmentIdentity sandbox={sandbox} />
              <StatusView sandbox={sandbox} threads={threads} active={tab === "status"} onError={setError} />
            </Stack>
          )}
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
