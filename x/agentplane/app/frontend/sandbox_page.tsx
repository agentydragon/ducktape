import { Badge, Button, Code, Group, Select, Stack, Switch, Table, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { create } from "@bufbuild/protobuf";

import { api, displayableError, listSessions, openSession, type Condition, type SandboxView } from "./client";
import { HarnessState, Provider, SessionSpecSchema, type SessionSummary } from "./protocol_pb";

const PROVIDER_ENUM: Record<string, Provider> = { claude: Provider.CLAUDE, codex: Provider.CODEX };

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
  const [sandbox, setSandbox] = useState<SandboxView | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState(() => `s-${Date.now().toString(36)}`);
  const [effort, setEffort] = useState("low");
  // The app's catalog of what this sandbox's harness may run; the thread carries the choice.
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const { data, error: failure } = await api.GET("/sandboxes/{name}", { params: { path: { name } } });
    if (failure) {
      setError(displayableError(failure));
      return;
    }
    setSandbox(data);
    if (data.state !== "running") return;
    try {
      setSessions(await listSessions(name));
      setError(null);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  }, [name]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const provider = sandbox?.provider;
  useEffect(() => {
    if (!provider) return;
    void (async () => {
      const { data, error: failure } = await api.GET("/models");
      if (failure) {
        setError(displayableError(failure));
        return;
      }
      const offered = data[provider];
      setModels(offered);
      setModel((current) => (current && offered.includes(current) ? current : (offered[0] ?? null)));
    })();
  }, [provider]);

  async function createSession(): Promise<void> {
    if (!sandbox || !model) return;
    try {
      await openSession(
        name,
        sessionId,
        create(SessionSpecSchema, {
          provider: PROVIDER_ENUM[sandbox.provider],
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
      </Group>
      {error && <Text c="red">{error}</Text>}
      {sandbox && sandbox.state !== "running" && (
        <Text>The sandbox is {sandbox.state}; sessions need a running Pod.</Text>
      )}
      {sandbox && <StatusView sandbox={sandbox} />}
      <Group align="flex-end">
        <TextInput label="Session id" value={sessionId} onChange={(e) => setSessionId(e.currentTarget.value)} />
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
            <Table.Th>Harness</Table.Th>
            <Table.Th>Active turn</Table.Th>
            <Table.Th>Events</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {sessions.map((session) => (
            <Table.Tr key={session.sessionId}>
              <Table.Td>
                <Button variant="subtle" onClick={() => onOpenSession(session.sessionId)}>
                  {session.sessionId}
                </Button>
              </Table.Td>
              <Table.Td>{HarnessState[session.harness]}</Table.Td>
              <Table.Td>{session.activeTurnId || "—"}</Table.Td>
              <Table.Td>{String(session.lastSequence)}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
