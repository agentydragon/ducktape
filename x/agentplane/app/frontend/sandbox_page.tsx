import { Badge, Button, Group, Select, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { api, displayableError, listSessions, openSession, type SandboxView, type SessionSummary } from "./client";

const PROVIDER_ENUM: Record<string, "PROVIDER_CLAUDE" | "PROVIDER_CODEX"> = {
  claude: "PROVIDER_CLAUDE",
  codex: "PROVIDER_CODEX",
};

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

  async function create(): Promise<void> {
    if (!sandbox) return;
    try {
      await openSession(name, sessionId, {
        provider: PROVIDER_ENUM[sandbox.provider],
        cwd: `/state/workspaces/${sessionId}`,
        model: sandbox.model,
        reasoningEffort: effort,
      });
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
      <Group align="flex-end">
        <TextInput label="Session id" value={sessionId} onChange={(e) => setSessionId(e.currentTarget.value)} />
        <Select
          label="Reasoning effort"
          data={["low", "medium", "high"]}
          value={effort}
          onChange={(v) => v && setEffort(v)}
        />
        <Button onClick={() => void create()} disabled={!sandbox || sandbox.state !== "running" || !sessionId}>
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
                <Button variant="subtle" onClick={() => onOpenSession(session.sessionId ?? "")}>
                  {session.sessionId}
                </Button>
              </Table.Td>
              <Table.Td>{session.harness}</Table.Td>
              <Table.Td>{session.activeTurnId || "—"}</Table.Td>
              <Table.Td>{session.lastSequence ?? "0"}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
