import { Badge, Button, Group, Select, Stack, Switch, Table, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { api, displayableError, type NewSandbox, type SandboxView } from "./client";

const REFRESH_MS = 5000;

const STATE_COLORS: Record<string, string> = {
  running: "green",
  suspended: "gray",
  archived: "gray",
  waiting_for_pod: "yellow",
  waiting_for_pod_ready: "yellow",
};

export function SandboxList({ onOpen }: { onOpen: (name: string) => void }): JSX.Element {
  const [rows, setRows] = useState<SandboxView[]>([]);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<NewSandbox>({ slug: "", provider: "claude", model: "" });

  const refresh = useCallback(async () => {
    const { data, error: failure } = await api.GET("/sandboxes", {
      params: { query: { include_archived: includeArchived } },
    });
    if (failure) setError(displayableError(failure));
    else {
      setRows(data);
      setError(null);
    }
  }, [includeArchived]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  async function act(name: string, action: "suspend" | "resume" | "archive" | "unarchive" | "delete"): Promise<void> {
    const params = { params: { path: { name } } };
    const { error: failure } =
      action === "delete"
        ? await api.DELETE("/sandboxes/{name}", params)
        : await api.POST(`/sandboxes/{name}/${action}`, params);
    if (failure) setError(displayableError(failure));
    await refresh();
  }

  async function create(): Promise<void> {
    const { error: failure } = await api.POST("/sandboxes", { body: form });
    if (failure) setError(displayableError(failure));
    else setForm({ ...form, slug: "" });
    await refresh();
  }

  return (
    <Stack>
      <Title order={2}>Sandboxes</Title>
      {error && <Text c="red">{error}</Text>}
      <Group align="flex-end">
        <TextInput label="Name" value={form.slug} onChange={(e) => setForm({ ...form, slug: e.currentTarget.value })} />
        <Select
          label="Provider"
          data={["claude", "codex"]}
          value={form.provider}
          onChange={(value) => value && setForm({ ...form, provider: value as NewSandbox["provider"] })}
        />
        <TextInput
          label="Model"
          value={form.model}
          onChange={(e) => setForm({ ...form, model: e.currentTarget.value })}
        />
        <Button onClick={() => void create()} disabled={!form.slug || !form.model}>
          New sandbox
        </Button>
        <Switch
          label="Show archived"
          checked={includeArchived}
          onChange={(e) => setIncludeArchived(e.currentTarget.checked)}
        />
      </Group>
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Name</Table.Th>
            <Table.Th>Provider</Table.Th>
            <Table.Th>Model</Table.Th>
            <Table.Th>State</Table.Th>
            <Table.Th>Node</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => (
            <Table.Tr key={row.name}>
              <Table.Td>
                <Button variant="subtle" onClick={() => onOpen(row.name)}>
                  {row.name}
                </Button>
              </Table.Td>
              <Table.Td>{row.provider}</Table.Td>
              <Table.Td>{row.model}</Table.Td>
              <Table.Td>
                <Badge color={STATE_COLORS[row.state] ?? "blue"}>{row.state}</Badge>
              </Table.Td>
              <Table.Td>
                {row.node_name ?? "—"} {row.pod?.ip ? `(${row.pod.ip})` : ""}
              </Table.Td>
              <Table.Td>
                <Group gap="xs">
                  {row.state === "suspended" || row.state === "archived" ? (
                    <Button size="xs" onClick={() => void act(row.name, "resume")}>
                      Resume
                    </Button>
                  ) : (
                    <Button size="xs" onClick={() => void act(row.name, "suspend")}>
                      Suspend
                    </Button>
                  )}
                  {row.archived ? (
                    <Button size="xs" variant="light" onClick={() => void act(row.name, "unarchive")}>
                      Unarchive
                    </Button>
                  ) : (
                    <Button size="xs" variant="light" onClick={() => void act(row.name, "archive")}>
                      Archive
                    </Button>
                  )}
                  <Button size="xs" color="red" variant="light" onClick={() => void act(row.name, "delete")}>
                    Delete
                  </Button>
                </Group>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
