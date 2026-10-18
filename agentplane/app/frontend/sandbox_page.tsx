import { NotificationStatus } from "./notification_status";
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Menu,
  Paper,
  Stack,
  Switch,
  Table,
  Tabs,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import IconPlus from "@tabler/icons-react/dist/esm/icons/IconPlus.mjs";
import IconTrash from "@tabler/icons-react/dist/esm/icons/IconTrash.mjs";
import { type JSX, useEffect, useEffectEvent, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import {
  api,
  displayableError,
  findThread,
  getSessionOpening,
  getRunnerStatus,
  getThread,
  harnessLabel,
  HttpError,
  openSession,
  type RunnerStatusView,
  type SandboxView,
  type ThreadView,
} from "./client";
import { ActionPolicySection, BoundPolicySection } from "./actions/policy";
import { EgressLog } from "./egress";
import { JsonView } from "./json_view";
import { MarkGlyph } from "./mark_glyph";
import { durationLabel } from "./format_time";
import { TimeAgo } from "./time_ago";
import { ConfirmDelete, deletable, DELETE_NEEDS_SUSPENDED, SuspendResume } from "./lifecycle";
import { liveSandboxUrl, useLive, type SandboxSnapshot } from "./live";
import { NewSessionForm } from "./new_session_form";
import { PolicyBindingsSection } from "./policy_bindings";
import { RawSwitch } from "./raw_switch";
import { SandboxConsole } from "./sandbox_console";
import { SessionsTable } from "./sandbox_sessions";
import {
  hasVmConsole,
  containerStatuses,
  podIp,
  podPhase,
  rawConditions,
  rawString,
  sandboxReady,
  sandboxStatusDetail,
  sandboxSummary,
  suspensionReason,
  type RawCondition,
} from "./sandbox_status";
import { SANDBOX_STATUS_MARKS } from "./status_mark";
import { snapshotFresh } from "./thread_status";
import { TopbarActions, TopbarTitle } from "./topbar";
import type { SessionSpec } from "../../proto/runner_pb";

// Persist only the Session ID, never instructions or setup scripts (which can contain secrets).
function pendingOpenKey(sandbox: string): string {
  return `agentplane:pending-open:${sandbox}`;
}

type PendingOpen = { sessionId: string; state?: string; reason?: string };

function openingStateLabel(state: string | undefined): string {
  switch (state) {
    case "DELIVERY_STATE_QUEUED_IN_DISPATCHER":
      return "Queued in Dispatcher";
    case "DELIVERY_STATE_DELIVERING_TO_RUNNER":
      return "Opening on the runner";
    case "DELIVERY_STATE_RUNNER_ADMITTED":
      return "Accepted by the runner";
    case "DELIVERY_STATE_FAILED":
      return "Opening failed";
    case "DELIVERY_STATE_UNKNOWN":
      return "Opening outcome unavailable";
    default:
      return "Waiting for opening status";
  }
}

// The page's tabs, named in the URL (`?tab=`) so a tab can be linked to and survives a reload.
const TABS = ["sessions", "egress_log", "policy", "status", "console"] as const;
type Tab = (typeof TABS)[number];
const DEFAULT_TAB: Tab = "sessions";

function isTab(value: string | null): value is Tab {
  return TABS.includes(value as Tab);
}

function ConditionsTable({ title, conditions }: { title: string; conditions: RawCondition[] }): JSX.Element {
  return (
    <Stack gap="xs">
      <Title order={5}>{title}</Title>
      <Table.ScrollContainer minWidth={620} type="native">
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Condition</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Reason</Table.Th>
              <Table.Th>Message</Table.Th>
              <Table.Th>Transition</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {conditions.map((condition) => (
              <Table.Tr key={condition.type}>
                <Table.Td>{condition.type}</Table.Td>
                <Table.Td>
                  <Badge color={condition.status === "True" ? "green" : "orange"}>{condition.status}</Badge>
                </Table.Td>
                <Table.Td>{condition.reason ?? "—"}</Table.Td>
                <Table.Td>{condition.message ?? "—"}</Table.Td>
                <Table.Td>
                  {condition.lastTransitionTime ?? "—"}
                  {condition.lastProbeTime ? ` · probed ${condition.lastProbeTime}` : ""}
                  {condition.observedGeneration !== null ? ` · generation ${condition.observedGeneration}` : ""}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Stack>
  );
}

function processStateLabel(state: RunnerStatusView["processes"][number]["state"]): string {
  switch (state) {
    case "PROCESS_STATE_STARTING":
      return "Starting";
    case "PROCESS_STATE_RUNNING":
      return "Running";
    case "PROCESS_STATE_STOPPING":
      return "Stopping";
  }
}

function executionStateLabel(state: RunnerStatusView["sessions"][number]["execution_state"]): string {
  switch (state) {
    case "SESSION_EXECUTION_STATE_DORMANT":
      return "Dormant";
    case "SESSION_EXECUTION_STATE_PREPARING":
      return "Preparing";
    case "SESSION_EXECUTION_STATE_READY":
      return "Ready";
    case "SESSION_EXECUTION_STATE_FAILED":
      return "Failed";
  }
}

type RunnerDiagnosticsState =
  | { kind: "loading" }
  | { kind: "available"; value: RunnerStatusView; observedAt: string }
  | { kind: "unavailable"; reason: string }
  | { kind: "suspended" };

function RunnerDiagnostics({ sandbox, active }: { sandbox: SandboxView; active: boolean }): JSX.Element {
  const [state, setState] = useState<RunnerDiagnosticsState>({ kind: "loading" });
  const [refreshVersion, setRefreshVersion] = useState(0);
  useEffect(() => {
    if (!active) return;
    if (sandbox.phase === "Suspended" || sandbox.operating_mode === "Suspended") {
      setState({ kind: "suspended" });
      return;
    }
    let current = true;
    setState({ kind: "loading" });
    void getRunnerStatus(sandbox.name).then(
      (value) => {
        if (current) setState({ kind: "available", value, observedAt: new Date().toISOString() });
      },
      (reason: unknown) => {
        if (current) setState({ kind: "unavailable", reason: displayableError(reason) });
      }
    );
    return () => {
      current = false;
    };
  }, [active, refreshVersion, sandbox.name, sandbox.operating_mode, sandbox.phase]);

  return (
    <Stack gap="xs" data-testid="sandbox-runner-diagnostics">
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="xs">
        <div>
          <Title order={5}>Runner diagnostics</Title>
          <Text size="xs" c="dimmed">
            Read-only snapshot of process and Session state reported by the runner.
          </Text>
        </div>
        <Button
          size="xs"
          variant="subtle"
          disabled={state.kind === "loading" || state.kind === "suspended"}
          onClick={() => setRefreshVersion((version) => version + 1)}
        >
          Refresh
        </Button>
      </Group>
      {state.kind === "loading" && (
        <Text size="sm" role="status" c="dimmed">
          Loading runner diagnostics…
        </Text>
      )}
      {state.kind === "suspended" && (
        <Text size="sm" role="status" c="dimmed">
          Sandbox suspended. Runner diagnostics are unavailable until it is running.
        </Text>
      )}
      {state.kind === "unavailable" && (
        <Text size="sm" role="status" c="dimmed">
          Runner diagnostics unavailable: {state.reason}
        </Text>
      )}
      {state.kind === "available" && (
        <Stack gap="xs">
          <Text size="xs" c="dimmed">
            Snapshot read at <time dateTime={state.observedAt}>{new Date(state.observedAt).toLocaleString()}</time>
          </Text>
          <Text size="sm">Runner boot ID · {state.value.runner_boot_id}</Text>
          {state.value.processes.length === 0 ? (
            <Text size="sm" c="dimmed">
              No harness processes reported.
            </Text>
          ) : (
            <Stack gap="xs">
              <Title order={6}>Harness processes</Title>
              {state.value.processes.map((process) => (
                <Paper key={process.process_id} withBorder p="sm" radius="md">
                  <Stack gap={4}>
                    <Group justify="space-between" wrap="wrap" gap="xs">
                      <Text fw={600}>{harnessLabel(process.harness)}</Text>
                      <Badge>{processStateLabel(process.state)}</Badge>
                    </Group>
                    <Text size="sm">PID {process.pid ?? "not started"}</Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Process ID · {process.process_id}
                    </Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Assigned Sessions ·{" "}
                      {process.assigned_session_ids.length ? process.assigned_session_ids.join(", ") : "None"}
                    </Text>
                  </Stack>
                </Paper>
              ))}
            </Stack>
          )}
          {state.value.sessions.length === 0 ? (
            <Text size="sm" c="dimmed">
              No Sessions reported.
            </Text>
          ) : (
            <Stack gap="xs">
              <Title order={6}>Sessions</Title>
              {state.value.sessions.map((session) => (
                <Paper key={session.session_id} withBorder p="sm" radius="md">
                  <Stack gap={4}>
                    <Group justify="space-between" wrap="wrap" gap="xs">
                      <Text fw={600} style={{ overflowWrap: "anywhere" }}>
                        {session.session_id}
                      </Text>
                      <Badge>{executionStateLabel(session.execution_state)}</Badge>
                    </Group>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Process ID · {session.process_id ?? "—"}
                    </Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Native Session ID · {session.native_session_id ?? "—"}
                    </Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Active turn · {session.active_turn_id ?? "—"}
                    </Text>
                    <Text size="xs" c="dimmed">
                      Failure cursor · {session.failure_cursor ?? "—"}
                      {session.busy ? " · Work in progress" : ""}
                    </Text>
                  </Stack>
                </Paper>
              ))}
            </Stack>
          )}
        </Stack>
      )}
    </Stack>
  );
}

/** What Kubernetes says about the sandbox: the Sandbox CR's own status, then its Pod's. */
function StatusView({ sandbox, active }: { sandbox: SandboxView; active: boolean }): JSX.Element {
  const [raw, setRaw] = useState(false);
  const sandboxConditions = rawConditions(sandbox.status);
  const podConditions = rawConditions(sandbox.pod?.status ?? null);
  const containers = containerStatuses(sandbox.pod?.status ?? null);
  return (
    <Stack gap="xs">
      <Group>
        <Title order={4}>Status</Title>
        <RawSwitch raw={raw} onChange={setRaw} />
      </Group>
      {raw ? (
        <JsonView value={sandbox} />
      ) : (
        <>
          <Text size="sm">
            Sandbox {sandbox.phase.toLowerCase()}
            {suspensionReason(sandbox) ? ` (${suspensionReason(sandbox)})` : ""}, created{" "}
            <TimeAgo value={sandbox.created_at} />
            {sandbox.pod?.node_name ? `, Pod placed on ${sandbox.pod.node_name}` : ", Pod not placed"}
          </Text>
          {/* The account its Pod runs as is what every binding names, so the tabs below are its policy, not this
              Sandbox's: two sandboxes sharing an account share what they may do. */}
          <Text size="sm">
            Runs as ServiceAccount {sandbox.service_account.namespace}/{sandbox.service_account.name}, the subject its
            egress and action-policy bindings name.
          </Text>
          {sandbox.preset && <Text size="sm">Made from preset {sandbox.preset}.</Text>}
          <Text size="sm">
            {sandbox.idle_suspend_after_seconds
              ? `Suspends once its runner has been idle for ${durationLabel(sandbox.idle_suspend_after_seconds)}.`
              : "Never suspends for idleness."}
          </Text>
          {sandbox.status && rawString(sandbox.status.phase) && (
            <Text size="sm">Sandbox status phase: {rawString(sandbox.status.phase)}</Text>
          )}
          {sandboxConditions.length > 0 ? (
            <ConditionsTable title="Sandbox conditions" conditions={sandboxConditions} />
          ) : (
            <Text size="sm" c="dimmed">
              No Sandbox conditions reported.
            </Text>
          )}
          {sandbox.pod ? (
            <>
              <Text size="sm">
                Pod {podPhase(sandbox) ?? "phase unknown"}
                {podIp(sandbox) ? ` at ${podIp(sandbox)}` : ""}
                {sandbox.pod.node_name ? ` on ${sandbox.pod.node_name}` : ""}
                {sandbox.pod.deleting ? " · deleting" : ""}
                {rawString(sandbox.pod.status?.reason) ? `: ${rawString(sandbox.pod.status?.reason)}` : ""}
                {rawString(sandbox.pod.status?.message) ? ` (${rawString(sandbox.pod.status?.message)})` : ""}
              </Text>
              <Text size="xs" c="dimmed">
                Pod {sandbox.pod.namespace}/{sandbox.pod.name} · UID {sandbox.pod.uid}
                {rawString(sandbox.pod.status?.startTime)
                  ? ` · started ${rawString(sandbox.pod.status?.startTime)}`
                  : ""}
                {rawString(sandbox.pod.status?.qosClass) ? ` · QoS ${rawString(sandbox.pod.status?.qosClass)}` : ""}
              </Text>
              {podConditions.length > 0 ? (
                <ConditionsTable title="Pod conditions" conditions={podConditions} />
              ) : (
                <Text size="sm" c="dimmed">
                  No Pod conditions reported.
                </Text>
              )}
              {containers.length > 0 && (
                <Stack gap="xs">
                  <Title order={5}>Containers</Title>
                  <Table.ScrollContainer minWidth={620} type="native">
                    <Table>
                      <Table.Thead>
                        <Table.Tr>
                          <Table.Th>Container</Table.Th>
                          <Table.Th>State</Table.Th>
                          <Table.Th>Ready</Table.Th>
                          <Table.Th>Restarts</Table.Th>
                          <Table.Th>Detail</Table.Th>
                          <Table.Th>Started</Table.Th>
                          <Table.Th>Finished</Table.Th>
                        </Table.Tr>
                      </Table.Thead>
                      <Table.Tbody>
                        {containers.map((container) => (
                          <Table.Tr key={`${container.role}/${container.name}`}>
                            <Table.Td>
                              {container.role === "container" ? container.name : `${container.role} ${container.name}`}
                            </Table.Td>
                            <Table.Td>
                              <Badge color={container.state === "running" ? "green" : "orange"}>
                                {container.state}
                              </Badge>
                            </Table.Td>
                            <Table.Td>{container.ready ? "yes" : "no"}</Table.Td>
                            <Table.Td>
                              {container.restartCount > 0 ? (
                                <Badge color="red">{container.restartCount}</Badge>
                              ) : (
                                container.restartCount
                              )}
                            </Table.Td>
                            <Table.Td>
                              {[
                                container.reason,
                                container.message,
                                container.exitCode !== null && `exit ${container.exitCode}`,
                              ]
                                .filter(Boolean)
                                .join(" · ") || "—"}
                            </Table.Td>
                            <Table.Td>{container.startedAt ?? "—"}</Table.Td>
                            <Table.Td>{container.finishedAt ?? "—"}</Table.Td>
                          </Table.Tr>
                        ))}
                      </Table.Tbody>
                    </Table>
                  </Table.ScrollContainer>
                </Stack>
              )}
            </>
          ) : (
            <Text size="sm">No Pod.</Text>
          )}
        </>
      )}
      <RunnerDiagnostics sandbox={sandbox} active={active} />
    </Stack>
  );
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
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const [error, setError] = useState<string | null>(null);
  const [creatingSession, setCreatingSession] = useState(false);
  const [pendingOpen, setPendingOpen] = useState<PendingOpen | null>(() => {
    const sessionId = sessionStorage.getItem(pendingOpenKey(name));
    return sessionId ? { sessionId } : null;
  });
  const resolvingOpen = useRef<string | null>(null);
  useEffect(() => {
    const sessionId = sessionStorage.getItem(pendingOpenKey(name));
    setPendingOpen(sessionId ? { sessionId } : null);
  }, [name]);
  function rememberOpen(attempt: PendingOpen | null): void {
    if (attempt) sessionStorage.setItem(pendingOpenKey(name), attempt.sessionId);
    else sessionStorage.removeItem(pendingOpenKey(name));
    setPendingOpen(attempt);
  }
  const [launching, setLaunching] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [includeArchived, setIncludeArchived] = useState(false);

  const live = useLive<SandboxSnapshot>(liveSandboxUrl(name, includeArchived), `Sandbox ${name}`);
  const sandbox: SandboxView | null = live.snapshot?.sandbox ?? null;
  const vmConsoleAvailable = hasVmConsole(sandbox);
  const tab: Tab =
    isTab(requestedTab) && (requestedTab !== "console" || vmConsoleAvailable) ? requestedTab : DEFAULT_TAB;
  const threadListing = live.snapshot?.threads ?? [];
  const threadsUnavailable = Array.isArray(threadListing) ? null : threadListing.code;
  const threads = Array.isArray(threadListing) ? threadListing : [];
  const ready = sandboxReady(sandbox);
  useEffect(() => {
    if (requestedTab === "console" && sandbox !== null && !vmConsoleAvailable) {
      setSearchParams({}, { replace: true });
    }
  }, [requestedTab, sandbox, setSearchParams, vmConsoleAvailable]);
  // History's retained Thread projection, keyed by its Session ID.
  const threadBySession: Record<string, ThreadView> = Object.fromEntries(threads.map((thread) => [thread.id, thread]));
  const pendingSessionId = pendingOpen?.sessionId;
  const projectedPendingThread = pendingSessionId ? threadBySession[pendingSessionId] : undefined;
  const visibleThreads = threads.filter((thread) => includeArchived || !thread.archived);
  const openProjectedPending = useEffectEvent((sessionId: string) => {
    void openThread(sessionId, sessionId);
  });

  // A queued Open is already durable, so the History projection stream is the event that completes
  // it. Match by the client-minted Session ID; unrelated Threads never resolve the pending attempt.
  useEffect(() => {
    if (!pendingSessionId) {
      resolvingOpen.current = null;
      return;
    }
    if (projectedPendingThread) openProjectedPending(pendingSessionId);
  }, [pendingSessionId, projectedPendingThread]);

  // No re-read after an action: the change reaches the API server, and the watch behind the
  // stream brings the sandbox's new state back on its own.
  async function act(action: "suspend" | "resume"): Promise<void> {
    const { error: failure } = await api.POST(`/sandboxes/{name}/${action}`, { params: { path: { name } } });
    setError(failure ? displayableError(failure) : null);
  }

  // No refresh after an action: the live stream carries the thread's new archived state back.
  async function threadAct(threadId: string, action: "archive" | "unarchive"): Promise<void> {
    const { error: failure } = await api.POST(`/threads/{thread_id}/${action}`, {
      params: { path: { thread_id: threadId } },
    });
    setError(failure ? displayableError(failure) : null);
  }

  /** Deleting leaves nothing to look at, so a deleted sandbox takes the view back to the list. */
  async function remove(): Promise<void> {
    const { error: failure } = await api.DELETE("/sandboxes/{name}", { params: { path: { name } } });
    if (!failure) {
      onBack();
      return;
    }
    setError(displayableError(failure));
  }

  async function createSession(spec: SessionSpec, setupScript: string): Promise<void> {
    if (!sandbox || creatingSession || pendingOpen) return;
    const sessionId = crypto.randomUUID();
    // Only the Session ID survives reload. In particular, never persist instructions or bootstrap.
    rememberOpen({ sessionId });
    setLaunching(false);
    setCreatingSession(true);
    setError(null);
    try {
      const opening = await openSession(name, sessionId, spec, setupScript);
      if (!opening.sessionId) throw new Error("Open returned without a Session ID");
      // The live projection may have resolved this attempt while the Open response was in flight.
      // Do not resurrect its pending record after navigation, or after the user forgot it.
      if (sessionStorage.getItem(pendingOpenKey(name)) !== sessionId) return;
      rememberOpen({ sessionId, state: opening.state, reason: opening.reason });
      if (
        opening.state === "DELIVERY_STATE_RUNNER_ADMITTED" &&
        (threadBySession[sessionId] || (await findThread(sessionId)))
      ) {
        await openThread(sessionId, sessionId);
      }
    } catch (reason: unknown) {
      // Outcome may be ambiguous. Do not replay Open or bootstrap, even in this tab.
      setError(`${displayableError(reason)} Check this attempt before starting another session.`);
    } finally {
      setCreatingSession(false);
    }
  }

  async function checkPendingOpen(): Promise<void> {
    if (!pendingOpen || creatingSession) return;
    setCreatingSession(true);
    setError(null);
    try {
      const { sessionId } = pendingOpen;
      if (threadBySession[sessionId] || (await findThread(sessionId))) {
        await openThread(sessionId, sessionId);
      } else {
        try {
          const opening = await getSessionOpening(name, sessionId);
          rememberOpen({ sessionId, state: opening.state, reason: opening.reason });
          setError(`${openingStateLabel(opening.state)}${opening.reason ? `: ${opening.reason}` : "."}`);
        } catch (reason: unknown) {
          if (reason instanceof HttpError && (reason.status === 404 || reason.status === 503)) {
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
      setCreatingSession(false);
    }
  }

  async function openThread(sessionId: string, resolvedAttempt?: string): Promise<void> {
    const attempt =
      resolvedAttempt ?? (sessionStorage.getItem(pendingOpenKey(name)) === sessionId ? sessionId : undefined);
    if (attempt) {
      if (sessionStorage.getItem(pendingOpenKey(name)) !== attempt || resolvingOpen.current === attempt) return;
      resolvingOpen.current = attempt;
    }
    try {
      const thread = threadBySession[sessionId] ?? (await getThread(sessionId));
      if (attempt) {
        if (sessionStorage.getItem(pendingOpenKey(name)) !== attempt) {
          resolvingOpen.current = null;
          return;
        }
        rememberOpen(null);
      }
      onOpenThread(thread.id);
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
          <NotificationStatus sandbox={name} opened={notificationsOpen} onClose={() => setNotificationsOpen(false)} />
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
              {/* The API refuses a running sandbox (inventory.py); the reason is shown beside the item. */}
              <Menu.Item
                color="red"
                leftSection={<IconTrash size={15} />}
                disabled={!deletable(sandbox)}
                onClick={() => setConfirmingDelete(true)}
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
          onCancel={() => setConfirmingDelete(false)}
          onConfirm={() => {
            setConfirmingDelete(false);
            void remove();
          }}
        />
      )}
      {error && (
        <Alert color="red" role="alert">
          {error}
        </Alert>
      )}
      {live.snapshot !== null && sandbox === null && (
        <Alert color="red" role="alert">
          There is no sandbox {name} any more.
        </Alert>
      )}
      {sandbox && !ready && (
        <Alert color="gray" role="status">
          Sandbox status: {sandboxSummary(sandbox).label}. New sessions need a ready Sandbox.
        </Alert>
      )}
      {pendingOpen && (
        <Alert color="yellow" role="status" title="Session opening">
          <Stack gap="xs">
            <Text size="sm">
              Open attempt {pendingOpen.sessionId}: {openingStateLabel(pendingOpen.state)}
              {pendingOpen.reason ? ` — ${pendingOpen.reason}` : ""}. Check before starting another session. An
              unconfirmed attempt is never retried automatically.
            </Text>
            <Group gap="xs">
              <Button disabled={creatingSession} variant="light" onClick={() => void checkPendingOpen()}>
                Check Open status
              </Button>
              <Button
                disabled={creatingSession}
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
          <Tabs.Tab value="sessions">Sessions</Tabs.Tab>
          <Tabs.Tab value="egress_log">Egress log</Tabs.Tab>
          <Tabs.Tab value="policy">Policies</Tabs.Tab>
          <Tabs.Tab value="status">Status</Tabs.Tab>
          {vmConsoleAvailable && <Tabs.Tab value="console">Console</Tabs.Tab>}
        </Tabs.List>
        <Tabs.Panel value="egress_log" pt="sm">
          <EgressLog name={name} />
        </Tabs.Panel>
        <Tabs.Panel value="policy" pt="sm">
          <Stack gap="md">
            <PolicyBindingsSection sandbox={name} bindings={live.snapshot?.bindings ?? null} />
            <Stack gap="xs">
              <Title order={5}>Action policy</Title>
              <ActionPolicySection policy={live.snapshot?.action_policy ?? null} />
            </Stack>
            <BoundPolicySection policy={live.snapshot?.bound_policy ?? null} />
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="status" pt="sm">
          {sandbox && <StatusView sandbox={sandbox} active={tab === "status"} />}
        </Tabs.Panel>
        {vmConsoleAvailable && (
          <Tabs.Panel value="console" pt="sm">
            {tab === "console" && <SandboxConsole sandbox={name} />}
          </Tabs.Panel>
        )}
        <Tabs.Panel value="sessions" pt="sm">
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
                New session
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
              <NewSessionForm
                presetName={sandbox?.preset ?? null}
                startable={ready && !pendingOpen}
                creating={creatingSession}
                onStart={(spec, setupScript) => void createSession(spec, setupScript)}
                onClose={() => setLaunching(false)}
                onError={setError}
              />
            </div>
            {live.snapshot !== null && visibleThreads.length === 0 && !threadsUnavailable ? (
              <Text c="dimmed">No sessions yet.</Text>
            ) : (
              <SessionsTable
                threads={visibleThreads}
                sandbox={sandbox}
                fresh={snapshotFresh(live)}
                onOpen={(sessionId) => void openThread(sessionId)}
                onArchive={(threadId, action) => void threadAct(threadId, action)}
              />
            )}
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
