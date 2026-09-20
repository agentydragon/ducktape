import { Group, Paper, Table, Text, Title } from "@mantine/core";
import type { JSX } from "react";

import type { ExampleDetailResponse } from "../lib/api/client";
import { recallColorClass } from "../lib/colors";
import { formatStatsWithCI } from "../lib/formatters";
import BackButton from "./BackButton";
import Breadcrumb from "./Breadcrumb";
import DefinitionIdLink from "../lib/DefinitionIdLink";
import FileLink from "../lib/FileLink";
import SnapshotLink from "../lib/SnapshotLink";

interface Props {
  data: ExampleDetailResponse;
}

function formatStatusCounts(counts: Record<string, number>): string {
  const parts: string[] = [];
  if (counts.completed) parts.push(`${counts.completed} completed`);
  if (counts.timed_out) parts.push(`${counts.timed_out} timed_out`);
  if (counts.in_progress) parts.push(`${counts.in_progress} in_progress`);
  return parts.join(", ") || "—";
}

export default function ExampleDetail({ data }: Props): JSX.Element {
  return (
    <div className="space-y-4">
      <Paper shadow="sm" p="md" className="dark:bg-gray-900">
        <Group mb="sm">
          <BackButton />
          <Title order={3} m={0}>
            Example Detail
          </Title>
        </Group>
        <Breadcrumb
          items={[
            { label: "Home", href: "/" },
            { label: "Examples", href: "/examples" },
            {
              label: `${data.snapshot_slug}/${data.example_kind}${data.files_hash ? `/${data.files_hash.substring(0, 8)}` : ""}`,
            },
          ]}
        />

        <div className="mt-4 space-y-4">
          <div className="grid grid-cols-2 gap-4 text-sm">
            <Text>
              <Text span c="dimmed">
                Snapshot:
              </Text>{" "}
              <SnapshotLink slug={data.snapshot_slug} />
            </Text>
            <Text>
              <Text span c="dimmed">
                Split:
              </Text>{" "}
              <span className="capitalize">{data.split}</span>
            </Text>
            <Text>
              <Text span c="dimmed">
                Kind:
              </Text>{" "}
              {data.example_kind}
            </Text>
            <Text>
              <Text span c="dimmed">
                Catchable Occurrences:
              </Text>{" "}
              {data.recall_denominator}
            </Text>
            {data.files_hash && (
              <Text className="col-span-2">
                <Text span c="dimmed">
                  Files Hash:
                </Text>{" "}
                <Text span ff="monospace" size="xs">
                  {data.files_hash}
                </Text>
              </Text>
            )}
          </div>

          {data.example_kind === "file_set" && data.files && (
            <div>
              <Text fw={500} size="sm" mb="xs">
                Files ({data.files.length})
              </Text>
              <ul className="space-y-1 text-xs text-gray-600 dark:text-gray-400">
                {data.files.map((file) => (
                  <li key={file}>
                    <FileLink snapshotSlug={data.snapshot_slug} filePath={file} />
                  </li>
                ))}
              </ul>
            </div>
          )}

          {data.credit_stats && (
            <Group pt="sm" className="border-t border-gray-200 dark:border-gray-700">
              <Text size="sm" c="dimmed">
                Aggregate Recall:
              </Text>
              <Text size="sm" className={recallColorClass(data.credit_stats.mean)}>
                {formatStatsWithCI(data.credit_stats)}
              </Text>
            </Group>
          )}
        </div>
      </Paper>

      {data.definitions.length > 0 ? (
        <Paper shadow="sm" p="md" className="dark:bg-gray-900">
          <Text fw={500} size="sm" mb="sm">
            Definitions ({data.definitions.length})
          </Text>
          <Table.ScrollContainer minWidth={680}>
            <Table striped highlightOnHover withTableBorder>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Definition</Table.Th>
                  <Table.Th>Model</Table.Th>
                  <Table.Th ta="right">Recall</Table.Th>
                  <Table.Th ta="right">N Runs</Table.Th>
                  <Table.Th>Status</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {data.definitions.map((definition) => (
                  <Table.Tr key={definition.image_digest}>
                    <Table.Td>
                      <DefinitionIdLink id={definition.image_digest} />
                    </Table.Td>
                    <Table.Td ff="monospace" c="dimmed" className="text-xs">
                      {definition.model}
                    </Table.Td>
                    <Table.Td ta="right" className={recallColorClass(definition.credit_stats?.mean)}>
                      {definition.credit_stats ? formatStatsWithCI(definition.credit_stats) : "—"}
                    </Table.Td>
                    <Table.Td ta="right" c="dimmed">
                      {definition.n_runs}
                    </Table.Td>
                    <Table.Td c="dimmed" className="text-xs">
                      {formatStatusCounts(definition.status_counts)}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Paper>
      ) : (
        <Paper shadow="sm" p="md" className="dark:bg-gray-900">
          <Text size="sm" c="dimmed">
            No definitions have been evaluated on this example yet.
          </Text>
        </Paper>
      )}
    </div>
  );
}
