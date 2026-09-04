import { Badge, Button, Code, Group, Select, Stack, Switch, Table, Tabs, Text, TextInput, Title } from "@mantine/core";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import { create } from "@bufbuild/protobuf";

import { api, displayableError, listSessions, openSession, type Condition, type SandboxView } from "./client";
import { EgressSection } from "./egress";
import { ConfirmDelete, DeleteButton, SuspendResume } from "./lifecycle";
import { liveSandboxUrl, LiveStatus, useLive, type SandboxSnapshot } from "./live";
import { HarnessState, Provider, SessionSpecSchema, type SessionSummary } from "./protocol_pb";

// The harness a session runs, as the API's catalog names it and as the protocol's enum spells it.
type Harness = "claude" | "codex";
const HARNESSES: Harness[] = ["claude", "codex"];
const PROVIDER_ENUM: Record<Harness, Provider> = { claude: Provider.CLAUDE, codex: Provider.CODEX };

// The page's tabs, named in the URL (`?tab=`) so a tab can be linked to and survives a reload.
const TABS = ["sessions", "egress", "status"] as const;
type Tab = (typeof TABS)[number];
const DEFAULT_TAB: Tab = "sessions";

function isTab(value: string | null): value is Tab {
  return TABS.includes(value as Tab);
}

function ConditionsTable({ conditions }: { conditions: Condition[] }): JSX.Element {
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Condition</Table.Th>
          <Table.Th>Status</Table.Th>
          <Table.Th>Reason</Table.Th>
          <Table.Th>Message</Table.Th>
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
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

/** What Kubernetes says about the sandbox: the Sandbox CR's own status, then its Pod's. */
function StatusView({ sandbox }: { sandbox: SandboxView }): JSX.Element {
  const [raw, setRaw] = useState(false);
  return (
    <Stack gap="xs">
      <Group>
        <Title order={4}>Status</Title>
        <Switch label="Raw" checked={raw} onChange={(e) => setRaw(e.currentTarget.checked)} />
      </Group>
      {raw ? (
        <Code block>{JSON.stringify(sandbox, null, 2)}</Code>
      ) : (
        <>
          <Text size="sm">
            Sandbox {sandbox.operating_mode.toLowerCase()}, created {new Date(sandbox.created_at).toLocaleString()}
            {sandbox.node_name ? `, placed on ${sandbox.node_name}` : ", not placed"}
          </Text>
          {sandbox.conditions.length > 0 && <ConditionsTable conditions={sandbox.conditions} />}
          {sandbox.pod ? (
            <>
              <Text size="sm">
                Pod {sandbox.pod.phase ?? "unknown"}
                {sandbox.pod.ip ? ` at ${sandbox.pod.ip}` : ""}
                {sandbox.pod.node_name ? ` on ${sandbox.pod.node_name}` : ""}
                {sandbox.pod.reason ? `: ${sandbox.pod.reason}` : ""}
                {sandbox.pod.message ? ` (${sandbox.pod.message})` : ""}
              </Text>
              {sandbox.pod.conditions.length > 0 && <ConditionsTable conditions={sandbox.pod.conditions} />}
              {sandbox.pod.containers.map((container) => (
                <Group key={container.name} gap="xs">
                  <Text size="sm" fw={600}>
                    {container.name}
                  </Text>
                  <Badge color={container.state === "running" ? "green" : "orange"}>{container.state}</Badge>
                  {container.ready && <Badge variant="light">ready</Badge>}
                  {container.restart_count > 0 && <Badge color="red">{container.restart_count} restarts</Badge>}
                  {container.reason && <Text size="sm">{container.reason}</Text>}
                  {container.message && (
                    <Text size="sm" c="dimmed">
                      {container.message}
                    </Text>
                  )}
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
  onOpenSession,
  onBack,
}: {
  name: string;
  onOpenSession: (sessionId: string) => void;
  onBack: () => void;
}): JSX.Element {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const tab: Tab = isTab(requestedTab) ? requestedTab : DEFAULT_TAB;
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState(() => `s-${Date.now().toString(36)}`);
  const [effort, setEffort] = useState("low");
  // The app's catalog of what this sandbox's harness may run; the thread carries the choice.
  const [harness, setHarness] = useState<Harness>("claude");
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const live = useLive<SandboxSnapshot>(liveSandboxUrl(name));
  const sandbox: SandboxView | null = live.snapshot?.sandbox ?? null;
  const threads = live.snapshot?.threads ?? [];
  // Thread names by session id: the store's copy, which outlives the runner's list.
  const names = Object.fromEntries(
    threads.flatMap((thread) => (thread.name ? [[thread.session_id, thread.name]] : []))
  );

  // The runner answers ListSessions per request and has no stream, so the table is re-read at the
  // moments that can change it: the Pod coming or going, and a session opening, which the store
  // records as a thread and the stream then pushes. A harness stopping is not among them; it shows
  // when the page next reads.
  const state = sandbox?.state;
  const openedSessions = threads.length;
  useEffect(() => {
    if (state !== "running") {
      setSessions([]);
      return;
    }
    listSessions(name).then(setSessions, (reason: unknown) => setError(displayableError(reason)));
  }, [name, state, openedSessions]);

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await api.GET("/models");
      if (failure) {
        setError(displayableError(failure));
        return;
      }
      const offered = data[harness];
      setModels(offered);
      setModel((current) => (current && offered.includes(current) ? current : (offered[0] ?? null)));
    })();
  }, [harness]);

  // No re-read after an action: the change reaches the API server, and the watch behind the
  // stream brings the sandbox's new state back on its own.
  async function act(action: "suspend" | "resume"): Promise<void> {
    const { error: failure } = await api.POST(`/sandboxes/{name}/${action}`, { params: { path: { name } } });
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
    if (!sandbox || !model) return;
    try {
      await openSession(
        name,
        sessionId,
        create(SessionSpecSchema, {
          provider: PROVIDER_ENUM[harness],
          cwd: `/state/workspaces/${sessionId}`,
          model,
          reasoningEffort: effort,
        })
      );
      onOpenSession(sessionId);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  }

  return (
    <Stack>
      <Group>
        <Button variant="subtle" onClick={onBack}>
          ← Sandboxes
        </Button>
        <Title order={2}>{name}</Title>
        {sandbox && <Badge>{sandbox.state}</Badge>}
        {sandbox && (
          <Group gap="xs" ml="auto" wrap="nowrap">
            <SuspendResume sandbox={sandbox} onAct={(action) => void act(action)} />
            <DeleteButton sandbox={sandbox} onDelete={() => setConfirmingDelete(true)} />
          </Group>
        )}
      </Group>
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
      {sandbox && sandbox.state !== "running" && (
        <Text>The sandbox is {sandbox.state}; sessions need a running Pod.</Text>
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
          <Tabs.Tab value="status">Status</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="egress" pt="sm">
          <EgressSection name={name} bindings={live.snapshot?.bindings ?? null} />
        </Tabs.Panel>
        <Tabs.Panel value="status" pt="sm">
          {sandbox && <StatusView sandbox={sandbox} />}
        </Tabs.Panel>
        <Tabs.Panel value="sessions" pt="sm">
          <Stack>
            <Group align="flex-end">
              <TextInput label="Session id" value={sessionId} onChange={(e) => setSessionId(e.currentTarget.value)} />
              <Select
                label="Harness"
                data={HARNESSES}
                value={harness}
                onChange={(v) => v && setHarness(v as Harness)}
              />
              <Select label="Model" data={models} value={model} onChange={setModel} />
              <Select
                label="Reasoning effort"
                data={["low", "medium", "high"]}
                value={effort}
                onChange={(v) => v && setEffort(v)}
              />
              <Button
                onClick={() => void createSession()}
                disabled={!sandbox || sandbox.state !== "running" || !sessionId || !model}
              >
                New session
              </Button>
            </Group>
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Session</Table.Th>
                  <Table.Th>Harness state</Table.Th>
                  <Table.Th>Active turn</Table.Th>
                  <Table.Th>Events</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {sessions.map((session) => (
                  <Table.Tr key={session.sessionId}>
                    <Table.Td>
                      <Group gap="xs">
                        <Button variant="subtle" onClick={() => onOpenSession(session.sessionId)}>
                          {session.sessionId}
                        </Button>
                        {names[session.sessionId] && <Text size="sm">{names[session.sessionId]}</Text>}
                      </Group>
                    </Table.Td>
                    <Table.Td>{HarnessState[session.harness]}</Table.Td>
                    <Table.Td>{session.activeTurnId || "—"}</Table.Td>
                    <Table.Td>{String(session.lastSequence)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
