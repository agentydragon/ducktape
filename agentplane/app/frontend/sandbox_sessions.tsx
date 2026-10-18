import { ActionIcon, Button, Group, Menu, Stack, Table, Text } from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import type { JSX } from "react";

import { harnessLabel, type SandboxView, type ThreadView } from "./client";
import { TimeAgo } from "./time_ago";
import { archiveBlockReason, threadStatusFromSnapshot } from "./thread_status";
import { ThreadStatusIndicator } from "./thread_status_indicator";

/** The one-line note under a Session's name while its setup script has not completed; null once it has, or had none. */
function setupNote(state: string | null | undefined): { text: string; failed: boolean } | null {
  switch (state) {
    case "SETUP_STATE_UNSPECIFIED":
    case "SETUP_STATE_NOT_REQUIRED":
    case "SETUP_STATE_SUCCEEDED":
    case null:
    case undefined:
      return null;
    case "SETUP_STATE_RUNNING":
      return { text: "Setup script running", failed: false };
    case "SETUP_STATE_FAILED":
      return { text: "Setup script failed", failed: true };
    case "SETUP_STATE_INTERRUPTED":
      return { text: "Setup script interrupted", failed: true };
    default:
      return { text: "Setup state unknown", failed: true };
  }
}

/** A Sandbox's Sessions, newest activity as History last projected it. */
export function SessionsTable({
  threads,
  sandbox,
  fresh,
  onOpen,
  onArchive,
}: {
  threads: ThreadView[];
  sandbox: SandboxView | null;
  /** Whether the snapshot's Thread states are being kept current. */
  fresh: boolean;
  onOpen: (sessionId: string) => void;
  onArchive: (sessionId: string, action: "archive" | "unarchive") => void;
}): JSX.Element {
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th />
          <Table.Th>Session</Table.Th>
          <Table.Th visibleFrom="sm">Harness</Table.Th>
          <Table.Th visibleFrom="sm">Last activity</Table.Th>
          <Table.Th />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {threads.map((thread) => {
          const status = threadStatusFromSnapshot(thread, sandbox ?? undefined, fresh);
          const setup = setupNote(thread.setup_state);
          const archiveBlock = archiveBlockReason(thread);
          const activity = thread.last_event_at ? <TimeAgo value={thread.last_event_at} /> : "No events yet";
          return (
            <Table.Tr key={thread.id}>
              <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                <ThreadStatusIndicator kind={status.kind} label={status.label} />
              </Table.Td>
              <Table.Td>
                {/* The id is only useful once you're in the session's own detail view (which
                  shows it beside the name); the list links by name where one exists. */}
                <Button variant="subtle" px="xs" onClick={() => onOpen(thread.id)}>
                  {thread.name ?? thread.id}
                </Button>
                {setup && (
                  <Text size="xs" c={setup.failed ? "red" : "dimmed"} px="xs">
                    {setup.text}
                  </Text>
                )}
                {/* On a phone the other columns fold under the name, leaving room for the actions. */}
                <Stack gap={0} px="xs" hiddenFrom="sm">
                  <Text size="xs" c="dimmed">
                    {harnessLabel(thread.harness)}
                    {thread.model ? ` · ${thread.model}` : ""}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {activity}
                  </Text>
                </Stack>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Text size="sm">{harnessLabel(thread.harness)}</Text>
                {thread.model && (
                  <Text size="xs" c="dimmed">
                    {thread.model}
                  </Text>
                )}
              </Table.Td>
              <Table.Td visibleFrom="sm">{activity}</Table.Td>
              <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                <Group justify="flex-end">
                  <Menu position="bottom-end">
                    <Menu.Target>
                      <ActionIcon variant="subtle" aria-label={`More actions for ${thread.id}`}>
                        <IconDotsVertical size={16} />
                      </ActionIcon>
                    </Menu.Target>
                    <Menu.Dropdown>
                      {thread.archived ? (
                        <Menu.Item onClick={() => onArchive(thread.id, "unarchive")}>Unarchive</Menu.Item>
                      ) : (
                        <Menu.Item
                          disabled={archiveBlock !== null}
                          title={archiveBlock ?? undefined}
                          onClick={() => onArchive(thread.id, "archive")}
                        >
                          {archiveBlock ?? "Archive"}
                        </Menu.Item>
                      )}
                    </Menu.Dropdown>
                  </Menu>
                </Group>
              </Table.Td>
            </Table.Tr>
          );
        })}
      </Table.Tbody>
    </Table>
  );
}
