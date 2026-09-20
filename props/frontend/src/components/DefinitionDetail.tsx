import { Button, Group, Paper, Table, Text, Title } from "@mantine/core";
import type { JSX } from "react";

import type { DefinitionDetailResponse } from "../lib/api/client";
import { recallColorClass } from "../lib/colors";
import { formatAge, formatStatsWithCI } from "../lib/formatters";
import { useRunModal } from "../lib/runModalContext";
import type { ExampleKind, Split } from "../lib/types";
import BackButton from "./BackButton";
import Breadcrumb from "./Breadcrumb";
import RunsBrowser from "./RunsBrowser";

interface Props {
  data: DefinitionDetailResponse;
}

const colGroups: { split: Split; kind: ExampleKind; label: string }[] = [
  { split: "valid", kind: "whole_snapshot", label: "Valid Whole" },
  { split: "valid", kind: "file_set", label: "Valid Partial" },
  { split: "train", kind: "whole_snapshot", label: "Train Whole" },
  { split: "train", kind: "file_set", label: "Train Partial" },
];

export default function DefinitionDetail({ data }: Props): JSX.Element {
  const runModal = useRunModal();

  return (
    <div className="space-y-4">
      <Paper shadow="sm" p="md" className="dark:bg-gray-900">
        <Group mb="sm">
          <BackButton />
          <Title order={3} m={0}>
            Definition Detail
          </Title>
          <Button ml="auto" onClick={() => runModal.open({ definitionId: data.image_digest })}>
            + New Run
          </Button>
        </Group>
        <Breadcrumb
          items={[
            { label: "Home", href: "/" },
            { label: "Definitions", href: "/" },
            { label: data.display_name ?? data.image_digest },
          ]}
        />
        <Group gap="sm" mt="md" wrap="wrap" className="text-sm">
          {data.display_name && <Text fw={600}>{data.display_name}</Text>}
          <Text ff="monospace" c="blue">
            {data.image_digest}
          </Text>
          <Text c="dimmed">{data.agent_type}</Text>
          <Text c="dimmed">{formatAge(data.created_at)}</Text>
        </Group>
      </Paper>

      <Paper shadow="sm" p="md" className="dark:bg-gray-900">
        <Text fw={500} size="sm" mb="sm">
          Recall by Split/Kind
        </Text>
        <Table.ScrollContainer minWidth={680}>
          <Table striped highlightOnHover withTableBorder>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Split/Kind</Table.Th>
                <Table.Th ta="right">Recall</Table.Th>
                <Table.Th ta="right">N</Table.Th>
                <Table.Th ta="right">Zero</Table.Th>
                <Table.Th ta="right">Completed</Table.Th>
                <Table.Th ta="right">Max Turns</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {colGroups.map(({ split, kind, label }) => {
                const stats = data.stats[split]?.[kind];
                return (
                  <Table.Tr key={`${split}-${kind}`}>
                    <Table.Td fw={500}>{label}</Table.Td>
                    {stats ? (
                      <>
                        <Table.Td ta="right" className={recallColorClass(stats.recall_stats?.mean)}>
                          {stats.recall_stats ? formatStatsWithCI(stats.recall_stats) : "—"}
                        </Table.Td>
                        <Table.Td ta="right">
                          {stats.n_examples}/{stats.total_available}
                        </Table.Td>
                        <Table.Td ta="right" c="dimmed">
                          {stats.zero_count}
                        </Table.Td>
                        <Table.Td ta="right">{stats.status_counts?.completed ?? 0}</Table.Td>
                        <Table.Td ta="right" c="dimmed">
                          {stats.status_counts?.timed_out ?? 0}
                        </Table.Td>
                      </>
                    ) : (
                      <>
                        <Table.Td ta="right" c="dimmed">
                          —
                        </Table.Td>
                        <Table.Td ta="right" c="dimmed">
                          —
                        </Table.Td>
                        <Table.Td ta="right" c="dimmed">
                          —
                        </Table.Td>
                        <Table.Td ta="right" c="dimmed">
                          —
                        </Table.Td>
                        <Table.Td ta="right" c="dimmed">
                          —
                        </Table.Td>
                      </>
                    )}
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Paper>

      <RunsBrowser initialDefinitionId={data.image_digest} />
    </div>
  );
}
