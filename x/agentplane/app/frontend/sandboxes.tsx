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
  Text,
  TextInput,
  Title,
} from "@mantine/core";
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
  const [form, setForm] = useState<NewSandbox>({ slug: "", provider: "claude" });

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
        <TextInput
          label="Name"
          value={form.slug}
          onChange={(e) => setForm({ ...form, slug: e.currentTarget.value })}
          style={{ flex: "1 1 10rem" }}
        />
        <Select
          label="Provider"
          data={["claude", "codex"]}
          value={form.provider}
          onChange={(value) => value && setForm({ ...form, provider: value as NewSandbox["provider"] })}
          style={{ flex: "1 1 8rem" }}
        />
        <Button onClick={() => void create()} disabled={!form.slug}>
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
            <Table.Th visibleFrom="sm">Provider</Table.Th>
            <Table.Th visibleFrom="sm">State</Table.Th>
            <Table.Th visibleFrom="sm">Node</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => {
            const node = `${row.node_name ?? "—"} ${row.pod?.ip ? `(${row.pod.ip})` : ""}`;
            const state = <Badge color={STATE_COLORS[row.state] ?? "blue"}>{row.state}</Badge>;
            return (
              <Table.Tr key={row.name}>
                <Table.Td>
                  <Button variant="subtle" px="xs" onClick={() => onOpen(row.name)}>
                    {row.name}
                  </Button>
                  {/* On a phone the other columns fold under the name, leaving room for the actions. */}
                  <Stack gap="xs" hiddenFrom="sm">
                    {state}
                    <Text size="xs" c="dimmed">
                      {row.provider} · {node}
                    </Text>
                  </Stack>
                </Table.Td>
                <Table.Td visibleFrom="sm">{row.provider}</Table.Td>
                <Table.Td visibleFrom="sm">{state}</Table.Td>
                <Table.Td visibleFrom="sm">{node}</Table.Td>
                <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  <Group gap="xs" wrap="nowrap" justify="flex-end">
                    {row.state === "suspended" || row.state === "archived" ? (
                      <Button size="xs" onClick={() => void act(row.name, "resume")}>
                        Resume
                      </Button>
                    ) : (
                      <Button size="xs" onClick={() => void act(row.name, "suspend")}>
                        Suspend
                      </Button>
                    )}
                    <Menu position="bottom-end">
                      <Menu.Target>
                        <ActionIcon variant="subtle" aria-label={`More actions for ${row.name}`}>
                          ⋯
                        </ActionIcon>
                      </Menu.Target>
                      <Menu.Dropdown>
                        {row.archived ? (
                          <Menu.Item onClick={() => void act(row.name, "unarchive")}>Unarchive</Menu.Item>
                        ) : (
                          <Menu.Item onClick={() => void act(row.name, "archive")}>Archive</Menu.Item>
                        )}
                        <Menu.Item color="red" onClick={() => void act(row.name, "delete")}>
                          Delete
                        </Menu.Item>
                      </Menu.Dropdown>
                    </Menu>
                  </Group>
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
