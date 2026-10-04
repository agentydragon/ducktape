import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Menu,
  Select,
  Stack,
  Switch,
  Table,
  Tabs,
  Text,
  TextInput,
  Textarea,
  Title,
} from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import { type JSX, useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import { fromJson, type JsonValue } from "@bufbuild/protobuf";

import {
  api,
  displayableError,
  findThread,
  listSessions,
  modelsForHarness,
  openSession,
  RunnerUnavailableError,
  type Harness,
  type ModelOption,
  type SandboxView,
  type ThreadView,
} from "./client";
import { ActionPolicySection } from "./actions/policy";
import { EgressSection } from "./egress";
import { JsonView } from "./json_view";
import { ConfirmDelete, DeleteButton, SuspendResume } from "./lifecycle";
import { liveSandboxUrl, LiveStatus, useLive, type SandboxSnapshot } from "./live";
import { RawSwitch } from "./raw_switch";
import {
  containerStatuses,
  podIp,
  podPhase,
  rawConditions,
  rawString,
  sandboxReady,
  sandboxSummary,
  type RawCondition,
} from "./sandbox_status";
import { SANDBOX_STATUS_MARKS } from "./status_mark";
import { StaleNotice } from "./stream_status";
import { TopbarTitle } from "./topbar";
import { HarnessState, SessionSpecSchema, SetupState, type SessionSummary } from "../../runner/protocol_pb";

const HARNESSES: { value: Harness; label: string }[] = [
  { value: "HARNESS_CLAUDE", label: "Claude" },
  { value: "HARNESS_CODEX", label: "Codex" },
];

function setupLabel(state: SetupState): string {
  switch (state) {
    case SetupState.UNSPECIFIED:
    case SetupState.NOT_REQUIRED:
      return "—";
    case SetupState.RUNNING:
      return "Running";
    case SetupState.SUCCEEDED:
      return "Complete";
    case SetupState.FAILED:
      return "Failed";
    case SetupState.INTERRUPTED:
      return "Interrupted";
    default:
      return "Unknown";
  }
}

// The page's tabs, named in the URL (`?tab=`) so a tab can be linked to and survives a reload.
const TABS = ["sessions", "egress", "policy", "status"] as const;
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

function KubernetesGrantStatus({ sandbox }: { sandbox: SandboxView }): JSX.Element {
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Title order={5}>Kubernetes grants</Title>
        {!sandbox.kubernetes_grants_ready && (
          <Badge color={sandbox.kubernetes_grant_error ? "red" : "yellow"}>
            {sandbox.kubernetes_grant_error ? "Error" : "Applying"}
          </Badge>
        )}
      </Group>
      {sandbox.kubernetes_grant_error && (
        <Text c="red" role="alert">
          {sandbox.kubernetes_grant_error}
        </Text>
      )}
      {sandbox.kubernetes_grants.length > 0 ? (
        sandbox.kubernetes_grants.map(({ name, grant }) => (
          <Text key={name} size="sm">
            {name} · {grant.kind}
            {grant.kind === "RoleBinding" ? ` · namespace ${grant.namespace}` : " · cluster"} → {grant.role_ref.kind}/
            {grant.role_ref.name}
          </Text>
        ))
      ) : (
        <Text size="sm" c="dimmed">
          {sandbox.kubernetes_grants_ready
            ? "No Kubernetes grants selected."
            : "Waiting for the selected grants to apply."}
        </Text>
      )}
    </Stack>
  );
}

/** What Kubernetes says about the sandbox: the Sandbox CR's own status, then its Pod's. */
function StatusView({ sandbox }: { sandbox: SandboxView }): JSX.Element {
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
            Sandbox {sandbox.operating_mode.toLowerCase()}, created {new Date(sandbox.created_at).toLocaleString()}
            {sandbox.pod?.node_name ? `, Pod placed on ${sandbox.pod.node_name}` : ", Pod not placed"}
          </Text>
          {/* The account its Pod runs as is what every binding names, so the tabs below are its policy, not this
              Sandbox's: two sandboxes sharing an account share what they may do. */}
          <Text size="sm">
            Runs as ServiceAccount {sandbox.service_account.namespace}/{sandbox.service_account.name}, the subject its
            egress and action-policy bindings name.
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
          <KubernetesGrantStatus sandbox={sandbox} />
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
              {containers.length > 0 && <Title order={5}>Containers</Title>}
              {containers.map((container) => (
                <Group key={`${container.role}/${container.name}`} gap="xs">
                  <Text size="sm" fw={600}>
                    {container.role === "container" ? container.name : `${container.role} ${container.name}`}
                  </Text>
                  <Badge color={container.state === "running" ? "green" : "orange"}>{container.state}</Badge>
                  {container.ready && <Badge variant="light">ready</Badge>}
                  {container.restartCount > 0 && <Badge color="red">{container.restartCount} restarts</Badge>}
                  {container.reason && <Text size="sm">{container.reason}</Text>}
                  {container.message && (
                    <Text size="sm" c="dimmed">
                      {container.message}
                    </Text>
                  )}
                  {container.exitCode !== null && <Text size="sm">exit {container.exitCode}</Text>}
                  {container.startedAt && <Text size="sm">started {container.startedAt}</Text>}
                  {container.finishedAt && <Text size="sm">finished {container.finishedAt}</Text>}
                </Group>
              ))}
            </>
          ) : (
            <Text size="sm">No Pod.</Text>
          )}
        </>
      )}
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
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const tab: Tab = isTab(requestedTab) ? requestedTab : DEFAULT_TAB;
  const [error, setError] = useState<string | null>(null);
  const [sessionList, setSessionList] = useState<"loading" | "waiting" | "ready" | { error: string }>("loading");
  const [creatingSession, setCreatingSession] = useState(false);
  const [effort, setEffort] = useState("low");
  const [instructions, setInstructions] = useState("");
  const [cwdTemplate, setCwdTemplate] = useState("/state/workspaces/{session_id}");
  const [setupScript, setSetupScript] = useState("");
  const [defaultsLabel, setDefaultsLabel] = useState<string | null>(null);
  // The app's catalog of what this sandbox's Harness may run; the thread carries the choice.
  const [harness, setHarness] = useState<Harness>("HARNESS_CLAUDE");
  const [models, setModels] = useState<ModelOption[]>([]);
  const [model, setModel] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [includeArchived, setIncludeArchived] = useState(false);

  const live = useLive<SandboxSnapshot>(liveSandboxUrl(name, includeArchived), `Sandbox ${name}`);
  const sandbox: SandboxView | null = live.snapshot?.sandbox ?? null;
  const threads = live.snapshot?.threads ?? [];
  // The store's copy of each session's thread, which outlives the runner's own list.
  const threadBySession: Record<string, ThreadView> = Object.fromEntries(
    threads.map((thread) => [thread.session_id, thread])
  );
  // Thread names by session id: the store's copy, which outlives the runner's list.
  const names = Object.fromEntries(
    threads.flatMap((thread) => (thread.name ? [[thread.session_id, thread.name]] : []))
  );
  const visibleSessions = sessions.filter(
    (session) => includeArchived || !threadBySession[session.sessionId]?.archived
  );

  // The runner answers ListSessions per request and has no stream, so the table is re-read at the
  // moments that can change it: the Pod coming or going, and a session opening, which the store
  // records as a thread and the stream then pushes. A harness stopping is not among them; it shows
  // when the page next reads.
  const ready = sandboxReady(sandbox);
  const currentPodIp = sandbox ? podIp(sandbox) : null;
  const podUid = sandbox?.pod?.uid;
  const openedSessions = threads.length;
  const bindingKey = JSON.stringify(sandbox?.binding ?? null);
  useEffect(() => {
    let cancelled = false;
    let retry: number | undefined;
    setSessionList("loading");
    if (!ready) {
      setSessions([]);
      setSessionList("waiting");
      return;
    }
    async function refresh(): Promise<void> {
      try {
        const rows = await listSessions(name);
        if (cancelled) return;
        setSessions(rows);
        setSessionList("ready");
      } catch (reason: unknown) {
        if (cancelled) return;
        if (reason instanceof RunnerUnavailableError) {
          setSessionList("waiting");
          retry = window.setTimeout(() => void refresh(), 2000);
        } else {
          setSessionList({ error: displayableError(reason) });
        }
      }
    }
    void refresh();
    return () => {
      cancelled = true;
      window.clearTimeout(retry);
    };
  }, [name, ready, currentPodIp, podUid, openedSessions]);

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await api.GET("/models");
      if (failure) {
        setError(displayableError(failure));
        return;
      }
      const offered = data ? modelsForHarness(data, harness) : [];
      setModels(offered);
      setModel((current) =>
        current && offered.some((option) => option.model === current) ? current : (offered[0]?.model ?? null)
      );
    })();
  }, [harness]);

  useEffect(() => {
    const option = models.find((entry) => entry.model === model);
    // A Sandbox binding can arrive before the model catalog; do not erase its default
    // while the selected model is not yet known to this catalog.
    if (option && !option.reasoning_efforts.includes(effort)) setEffort(option.reasoning_efforts[0] ?? "");
  }, [effort, model, models]);

  useEffect(() => {
    const binding = sandbox?.binding;
    if (!binding) {
      setDefaultsLabel(null);
      setCwdTemplate("/state/workspaces/{session_id}");
      setSetupScript("");
      return;
    }
    const defaults = binding.session_defaults;
    setDefaultsLabel(defaults ? "Sandbox defaults" : null);
    if (!defaults) {
      setCwdTemplate("/state/workspaces/{session_id}");
      setSetupScript("");
      return;
    }
    if (defaults.harness) setHarness(defaults.harness);
    if (defaults.model) setModel(defaults.model);
    if (defaults.reasoning_effort) setEffort(defaults.reasoning_effort);
    setInstructions(defaults.instructions ?? "");
    setCwdTemplate(defaults.cwd ?? "/state/workspaces/{session_id}");
    setSetupScript(defaults.setup_script ?? "");
  }, [bindingKey]);

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

  async function createSession(): Promise<void> {
    if (!sandbox || !model || creatingSession) return;
    setCreatingSession(true);
    setError(null);
    // The operator does not pick a session id; the client mints one fresh for each attempt so a
    // retry after failure never collides with the one that just failed.
    const sessionId = `s-${crypto.randomUUID()}`;
    try {
      await openSession(
        name,
        sessionId,
        fromJson(SessionSpecSchema, {
          harness,
          cwd: cwdTemplate.replaceAll("{session_id}", sessionId),
          model,
          reasoningEffort: effort,
          instructions,
        } as JsonValue),
        setupScript
      );
      await openThread(sessionId);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    } finally {
      setCreatingSession(false);
    }
  }

  async function openThread(sessionId: string): Promise<void> {
    try {
      const thread = threadBySession[sessionId] ?? (await findThread(name, sessionId));
      if (!thread) throw new Error(`Thread metadata is not available for session ${sessionId}`);
      onOpenThread(thread.id);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  }

  return (
    <Stack>
      <TopbarTitle>
        <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
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
          {sandbox && (
            <Badge color={SANDBOX_STATUS_MARKS[sandboxSummary(sandbox).kind].color} style={{ flexShrink: 0 }}>
              {sandboxSummary(sandbox).label}
            </Badge>
          )}
          {defaultsLabel && (
            <Badge variant="light" style={{ flexShrink: 0 }}>
              {defaultsLabel}
            </Badge>
          )}
        </Group>
      </TopbarTitle>
      <Group>
        <Button variant="subtle" onClick={onBack}>
          ← Sandboxes
        </Button>
        {sandbox && (
          <Group gap="xs" ml="auto" wrap="nowrap">
            <SuspendResume sandbox={sandbox} onAct={(action) => void act(action)} />
            <DeleteButton sandbox={sandbox} onDelete={() => setConfirmingDelete(true)} />
          </Group>
        )}
      </Group>
      <StaleNotice streams={[live.stream]} />
      <LiveStatus live={live} />
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
      {error && <Text c="red">{error}</Text>}
      {live.snapshot !== null && sandbox === null && <Text c="red">There is no sandbox {name} any more.</Text>}
      {sandbox && !ready && (
        <Text>
          {sandbox.launch_grants_pending || !sandbox.kubernetes_grants_ready
            ? "Launch or Kubernetes grants are not ready; sessions cannot start yet."
            : `Sandbox status: ${sandboxSummary(sandbox).label}. Sessions need a ready Pod.`}
        </Text>
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
          <Tabs.Tab value="egress">Egress</Tabs.Tab>
          <Tabs.Tab value="policy">Action policy</Tabs.Tab>
          <Tabs.Tab value="status">Status</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="egress" pt="sm">
          <EgressSection name={name} bindings={live.snapshot?.bindings ?? null} />
        </Tabs.Panel>
        <Tabs.Panel value="policy" pt="sm">
          <ActionPolicySection policy={live.snapshot?.action_policy ?? null} />
        </Tabs.Panel>
        <Tabs.Panel value="status" pt="sm">
          {sandbox && <StatusView sandbox={sandbox} />}
        </Tabs.Panel>
        <Tabs.Panel value="sessions" pt="sm">
          <Stack>
            {ready && sessionList === "loading" && <Text role="status">Loading sessions…</Text>}
            {ready && sessionList === "waiting" && (
              <Text role="status">Waiting for the sandbox runner to become available; retrying automatically…</Text>
            )}
            {typeof sessionList === "object" && <Text c="red">{sessionList.error}</Text>}
            <Group align="flex-end">
              <Select
                label="Harness"
                data={HARNESSES}
                value={harness}
                onChange={(value) => value && setHarness(value as Harness)}
              />
              <Select
                label="Model"
                data={models.map((option) => ({ value: option.model, label: option.display_name }))}
                value={model}
                onChange={setModel}
              />
              {(models.find((option) => option.model === model)?.reasoning_efforts ?? []).length > 0 && (
                <Select
                  label="Reasoning effort"
                  data={models.find((option) => option.model === model)?.reasoning_efforts ?? []}
                  value={effort}
                  onChange={(v) => v && setEffort(v)}
                />
              )}
              <Button
                onClick={() => void createSession()}
                loading={creatingSession}
                disabled={!ready || sessionList !== "ready" || !model}
              >
                {creatingSession ? "Creating session…" : "New session"}
              </Button>
            </Group>
            <Textarea
              label="Standing instructions"
              description={defaultsLabel ? "Inherited from this sandbox; editable for this thread" : undefined}
              autosize
              minRows={2}
              value={instructions}
              onChange={(event) => setInstructions(event.currentTarget.value)}
            />
            <TextInput
              label="Working directory"
              description="{session_id} is replaced with this Thread's ID"
              value={cwdTemplate}
              onChange={(event) => setCwdTemplate(event.currentTarget.value)}
            />
            <Textarea
              label="Thread setup script"
              description="Runs once in the resolved working directory before the harness starts"
              autosize
              minRows={2}
              value={setupScript}
              onChange={(event) => setSetupScript(event.currentTarget.value)}
            />
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Session</Table.Th>
                  <Table.Th>Harness state</Table.Th>
                  <Table.Th>Setup</Table.Th>
                  <Table.Th>Active turn</Table.Th>
                  <Table.Th>Events</Table.Th>
                  <Table.Th />
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {visibleSessions.map((session) => {
                  const thread = threadBySession[session.sessionId];
                  return (
                    <Table.Tr key={session.sessionId}>
                      <Table.Td>
                        {/* The id is only useful once you're in the session's own detail view (which
                          shows it beside the name); the list links by name where one exists. */}
                        <Button variant="subtle" onClick={() => void openThread(session.sessionId)}>
                          {names[session.sessionId] ?? session.sessionId}
                        </Button>
                      </Table.Td>
                      <Table.Td>{HarnessState[session.harnessState]}</Table.Td>
                      <Table.Td>{setupLabel(session.setupState)}</Table.Td>
                      <Table.Td>{session.activeTurnId || "—"}</Table.Td>
                      <Table.Td>{String(session.lastCursor)}</Table.Td>
                      <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                        {thread && (
                          <Menu position="bottom-end">
                            <Menu.Target>
                              <ActionIcon variant="subtle" aria-label={`More actions for ${session.sessionId}`}>
                                <IconDotsVertical size={16} />
                              </ActionIcon>
                            </Menu.Target>
                            <Menu.Dropdown>
                              {thread.archived ? (
                                <Menu.Item onClick={() => void threadAct(thread.id, "unarchive")}>Unarchive</Menu.Item>
                              ) : (
                                <Menu.Item
                                  disabled={session.harnessState === HarnessState.RUNNING}
                                  onClick={() => void threadAct(thread.id, "archive")}
                                >
                                  {session.harnessState === HarnessState.RUNNING
                                    ? "Stop harness before archiving"
                                    : "Archive"}
                                </Menu.Item>
                              )}
                            </Menu.Dropdown>
                          </Menu>
                        )}
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
            <Group justify="flex-end">
              <Switch
                size="md"
                label="Show archived"
                checked={includeArchived}
                onChange={(e) => setIncludeArchived(e.currentTarget.checked)}
              />
            </Group>
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
