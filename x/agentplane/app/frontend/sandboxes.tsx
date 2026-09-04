import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Menu,
  MultiSelect,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import { useEffect, useState } from "react";

import { api, displayableError, type Condition, type NewSandbox, type SandboxView } from "./client";
import { ConfirmDelete, deletable, SuspendResume } from "./lifecycle";
import { liveSandboxesUrl, LiveStatus, useLive, type SandboxesSnapshot } from "./live";

const EMPTY_FORM: NewSandbox = { slug: "", policies: [] };

const STATE_COLORS: Record<string, string> = {
  running: "green",
  suspended: "gray",
  archived: "gray",
  waiting_for_pod: "yellow",
  waiting_for_pod_ready: "yellow",
};

function conditionLine({ type, status, reason, message }: Condition): string {
  return [`${type}=${status}`, reason, message].filter((part) => part).join(" · ");
}

/** The State badge's hover detail: the Sandbox's own conditions, then the Pod's phase and containers. */
function stateDetail(row: SandboxView): string {
  const lines = row.conditions.map(conditionLine);
  if (row.pod) {
    lines.push(
      [`Pod ${row.pod.phase ?? "unknown"}`, row.pod.reason, row.pod.message].filter((part) => part).join(" · ")
    );
    for (const container of row.pod.containers) {
      lines.push(
        [
          `${container.name}: ${container.state}`,
          container.reason,
          container.message,
          container.restart_count > 0 ? `${container.restart_count} restarts` : null,
        ]
          .filter((part) => part)
          .join(" · ")
      );
    }
  }
  return lines.length > 0 ? lines.join("\n") : "No conditions reported";
}

function StateBadge({ row }: { row: SandboxView }): JSX.Element {
  return (
    <Tooltip label={stateDetail(row)} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <Badge color={STATE_COLORS[row.state] ?? "blue"}>{row.state}</Badge>
    </Tooltip>
  );
}

export function SandboxList({ onOpen }: { onOpen: (name: string) => void }): JSX.Element {
  const [includeArchived, setIncludeArchived] = useState(false);
  // The list is pushed; an action's own failure is what this holds.
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<NewSandbox>(EMPTY_FORM);
  // The namespace's policies; ticking some grants them to this sandbox alone.
  const [policies, setPolicies] = useState<string[]>([]);
  // The sandbox whose deletion is being confirmed, by name; deleting takes its volume with it.
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const live = useLive<SandboxesSnapshot>(liveSandboxesUrl(includeArchived));
  const rows: SandboxView[] = live.snapshot?.sandboxes ?? [];

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await api.GET("/egress/policies");
      if (failure) setError(displayableError(failure));
      else setPolicies(data.map((policy) => policy.name));
    })();
  }, []);

  // No refresh after an action: the change reaches the API server, and the watch behind the
  // stream brings the new row back on its own.
  async function act(name: string, action: "suspend" | "resume" | "archive" | "unarchive" | "delete"): Promise<void> {
    const params = { params: { path: { name } } };
    const { error: failure } =
      action === "delete"
        ? await api.DELETE("/sandboxes/{name}", params)
        : await api.POST(`/sandboxes/{name}/${action}`, params);
    setError(failure ? displayableError(failure) : null);
  }

  async function create(): Promise<void> {
    const { error: failure } = await api.POST("/sandboxes", {
      body: form,
    });
    if (failure) setError(displayableError(failure));
    else {
      setForm(EMPTY_FORM);
      setError(null);
    }
  }

  return (
    <Stack>
      <Title order={2}>Sandboxes</Title>
      <LiveStatus live={live} />
      {confirmingDelete !== null && (
        <ConfirmDelete
          name={confirmingDelete}
          onCancel={() => setConfirmingDelete(null)}
          onConfirm={() => {
            void act(confirmingDelete, "delete");
            setConfirmingDelete(null);
          }}
        />
      )}
      {error && <Text c="red">{error}</Text>}
      <Group align="flex-end">
        <TextInput
          label="Name"
          value={form.slug}
          onChange={(e) => setForm({ ...form, slug: e.currentTarget.value })}
          style={{ flex: "1 1 10rem" }}
        />
        <MultiSelect
          label="Policies"
          description="What this sandbox may reach"
          data={policies}
          value={form.policies ?? []}
          onChange={(picked) => setForm({ ...form, policies: picked })}
          style={{ flex: "1 1 12rem" }}
        />
        <Button onClick={() => void create()} disabled={!form.slug}>
          New sandbox
        </Button>
      </Group>
      <Group justify="flex-end">
        <Switch
          size="md"
          label="Show archived"
          checked={includeArchived}
          onChange={(e) => setIncludeArchived(e.currentTarget.checked)}
        />
      </Group>
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Name</Table.Th>
            <Table.Th visibleFrom="sm">State</Table.Th>
            <Table.Th visibleFrom="sm">Node</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => {
            const node = `${row.node_name ?? "—"} ${row.pod?.ip ? `(${row.pod.ip})` : ""}`;
            const state = <StateBadge row={row} />;
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
                      {node}
                    </Text>
                  </Stack>
                </Table.Td>
                <Table.Td visibleFrom="sm">{state}</Table.Td>
                <Table.Td visibleFrom="sm">{node}</Table.Td>
                <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  <Group gap="xs" wrap="nowrap" justify="flex-end">
                    <SuspendResume sandbox={row} onAct={(action) => void act(row.name, action)} />
                    <Menu position="bottom-end">
                      <Menu.Target>
                        <ActionIcon variant="subtle" aria-label={`More actions for ${row.name}`}>
                          <IconDotsVertical size={16} />
                        </ActionIcon>
                      </Menu.Target>
                      <Menu.Dropdown>
                        {row.archived ? (
                          <Menu.Item onClick={() => void act(row.name, "unarchive")}>Unarchive</Menu.Item>
                        ) : (
                          <Menu.Item onClick={() => void act(row.name, "archive")}>Archive</Menu.Item>
                        )}
                        {/* The API refuses a running sandbox (inventory.py); suspend is one click left. */}
                        <Menu.Item color="red" disabled={!deletable(row)} onClick={() => setConfirmingDelete(row.name)}>
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
      {live.snapshot === null && <Text c="dimmed">Waiting for the first update…</Text>}
    </Stack>
  );
}
