import { ActionIcon, Button, Group, Menu, Stack, Table, Text } from "@mantine/core";
import type { TraceSearchSettings } from "../app_config_client";
import type { ThreadView } from "./client";
import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import { IconDotsVertical } from "../icons";
import type { JSX } from "react";

import { harnessLabel } from "../model_catalog";
import { attachedHarness } from "../harness_behavior";
import { traceSearchUrl } from "../trace_search";

import { TimeAgo } from "../time_ago";
import { archiveBlockReason, threadStatusFromSnapshot } from "./thread_status";
import { ThreadStatusIndicator } from "./thread_status_indicator";
import { SetupState } from "../../../proto/session_pb";
import { timestampLabel } from "./thread_presentation";

/** The one-line note under a Thread's name while its setup script has not completed; null once it has, or had none. */
function setupNote(state: SetupState | undefined): { text: string; failed: boolean } | null {
  switch (state) {
    case SetupState.UNSPECIFIED:
    case SetupState.NOT_REQUIRED:
    case SetupState.SUCCEEDED:
    case undefined:
      return null;
    case SetupState.RUNNING:
      return { text: "Setup script running", failed: false };
    case SetupState.FAILED:
      return { text: "Setup script failed", failed: true };
    case SetupState.INTERRUPTED:
      return { text: "Setup script interrupted", failed: true };
    default:
      return { text: "Setup state unknown", failed: true };
  }
}

/** A Sandbox's Threads, newest activity as History last projected it. */
export function SandboxThreadsTable({
  threads,
  sandbox,
  traceSearch,
  fresh,
  onOpen,
  onArchive,
}: {
  threads: ThreadView[];
  sandbox: EnvironmentDescriptor | null;
  traceSearch: TraceSearchSettings | null;
  /** Whether the snapshot's Thread states are being kept current. */
  fresh: boolean;
  onOpen: (threadId: string) => void;
  onArchive: (threadId: string, action: "archive" | "unarchive") => void;
}): JSX.Element {
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th />
          <Table.Th>Thread</Table.Th>
          <Table.Th visibleFrom="sm">Harness</Table.Th>
          <Table.Th visibleFrom="sm">Last activity</Table.Th>
          <Table.Th />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {threads.map((thread) => {
          const threadId = thread.metadata.threadId;
          const metadata = thread.metadata;
          const summary = thread.summary;
          const spec = summary.attached?.spec;
          const status = threadStatusFromSnapshot(thread, sandbox ?? undefined, fresh);
          const setup = setupNote(summary.attached?.setupState);
          const archiveBlock = archiveBlockReason(thread);
          const lastEventAt = timestampLabel(summary.lastEventAt);
          const activity = lastEventAt ? <TimeAgo value={lastEventAt} /> : "No events yet";
          return (
            <Table.Tr key={threadId}>
              <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                <ThreadStatusIndicator kind={status.kind} label={status.label} />
              </Table.Td>
              <Table.Td>
                {/* The id is only useful once you're in the Thread's own detail view (which
                  shows it beside the name); the list links by name where one exists. */}
                <Button variant="subtle" px="xs" onClick={() => onOpen(threadId)}>
                  {metadata.name ?? threadId}
                </Button>
                {setup && (
                  <Text size="xs" c={setup.failed ? "red" : "dimmed"} px="xs">
                    {setup.text}
                  </Text>
                )}
                {/* On a phone the other columns fold under the name, leaving room for the actions. */}
                <Stack gap={0} px="xs" hiddenFrom="sm">
                  <Text size="xs" c="dimmed">
                    {harnessLabel(attachedHarness(summary.attached))}
                    {spec?.model ? ` · ${spec.model}` : ""}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {activity}
                  </Text>
                </Stack>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Text size="sm">{harnessLabel(attachedHarness(summary.attached))}</Text>
                {spec?.model && (
                  <Text size="xs" c="dimmed">
                    {spec.model}
                  </Text>
                )}
              </Table.Td>
              <Table.Td visibleFrom="sm">{activity}</Table.Td>
              <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                <Group justify="flex-end">
                  <Menu position="bottom-end">
                    <Menu.Target>
                      <ActionIcon variant="subtle" aria-label={`More actions for ${threadId}`}>
                        <IconDotsVertical size={16} />
                      </ActionIcon>
                    </Menu.Target>
                    <Menu.Dropdown>
                      {traceSearch && (
                        <Menu.Item
                          component="a"
                          href={traceSearchUrl(traceSearch, { kind: "session", id: threadId })}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Find Thread traces in Grafana
                        </Menu.Item>
                      )}
                      {metadata.archived ? (
                        <Menu.Item onClick={() => onArchive(threadId, "unarchive")}>Unarchive</Menu.Item>
                      ) : (
                        <Menu.Item
                          disabled={archiveBlock !== null}
                          title={archiveBlock ?? undefined}
                          onClick={() => onArchive(threadId, "archive")}
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
