import { forwardRef, useImperativeHandle, useMemo } from "react";
import { Paper, Table, Text } from "@mantine/core";

export interface OccurrenceStatsRow {
  snapshot_slug: string;
  split: string;
  tp_id: string;
  occurrence_id: string;
  n_runs: number;
  mean_credit: number;
  min_credit: number;
  max_credit: number;
}

interface Props {
  occurrences: OccurrenceStatsRow[];
}

export interface OccurrenceStatsHandle {
  getStats(tpId: string, occurrenceId: string): OccurrenceStatsRow | undefined;
}

function pctParts(value: number): { integer: string; fraction: string } {
  const [integer, fraction] = (value * 100).toFixed(1).split(".");
  return { integer, fraction: `.${fraction}%` };
}

const OccurrenceStats = forwardRef<OccurrenceStatsHandle, Props>(function OccurrenceStats({ occurrences }, ref) {
  const byKey = useMemo(
    () => new Map(occurrences.map((occurrence) => [`${occurrence.tp_id}:${occurrence.occurrence_id}`, occurrence])),
    [occurrences]
  );
  useImperativeHandle(ref, () => ({ getStats: (tpId, occurrenceId) => byKey.get(`${tpId}:${occurrenceId}`) }), [byKey]);

  return (
    <Paper withBorder radius="md" className="bg-white dark:bg-gray-900 dark:border-gray-700">
      <div className="px-4 py-3 border-b dark:border-gray-700">
        <Text component="h3" size="sm" fw={600}>
          Occurrence Detection Statistics
        </Text>
        <Text size="xs" c="dimmed" mt={4}>
          Mean credit per occurrence across all critic runs. Low values indicate hard-to-find issues.
        </Text>
      </div>
      {occurrences.length === 0 ? (
        <Text size="sm" c="dimmed" p="md">
          No occurrence statistics available
        </Text>
      ) : (
        <div className="overflow-x-auto">
          <Table horizontalSpacing={0} verticalSpacing={0} className="w-full text-sm">
            <Table.Thead>
              <Table.Tr className="border-b dark:border-gray-700 bg-gray-50 dark:bg-gray-800">
                <Table.Th className="px-3 py-2 text-left font-medium text-gray-600 dark:text-gray-400">TP ID</Table.Th>
                <Table.Th className="px-3 py-2 text-left font-medium text-gray-600 dark:text-gray-400">
                  Occurrence
                </Table.Th>
                <Table.Th className="px-3 py-2 text-right font-medium text-gray-600 dark:text-gray-400">Mean</Table.Th>
                <Table.Th className="px-3 py-2 text-right font-medium text-gray-600 dark:text-gray-400">Min</Table.Th>
                <Table.Th className="px-3 py-2 text-right font-medium text-gray-600 dark:text-gray-400">Max</Table.Th>
                <Table.Th className="px-3 py-2 text-right font-medium text-gray-600 dark:text-gray-400">Runs</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {occurrences.map((occurrence) => {
                const mean = pctParts(occurrence.mean_credit);
                const min = pctParts(occurrence.min_credit);
                const max = pctParts(occurrence.max_credit);
                return (
                  <Table.Tr
                    key={`${occurrence.tp_id}:${occurrence.occurrence_id}`}
                    className="border-b dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800"
                  >
                    <Table.Td className="px-3 py-2 font-mono text-xs">{occurrence.tp_id}</Table.Td>
                    <Table.Td className="px-3 py-2 font-mono text-xs">{occurrence.occurrence_id}</Table.Td>
                    <Table.Td className="px-3 py-2 font-mono text-xs tabular-nums">
                      <span className="inline-block w-[3ch] text-right">{mean.integer}</span>
                      {mean.fraction}
                    </Table.Td>
                    <Table.Td className="px-3 py-2 font-mono text-xs tabular-nums">
                      <span className="inline-block w-[3ch] text-right">{min.integer}</span>
                      {min.fraction}
                    </Table.Td>
                    <Table.Td className="px-3 py-2 font-mono text-xs tabular-nums">
                      <span className="inline-block w-[3ch] text-right">{max.integer}</span>
                      {max.fraction}
                    </Table.Td>
                    <Table.Td className="px-3 py-2 text-right text-xs text-gray-600 dark:text-gray-400">
                      {occurrence.n_runs}
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </div>
      )}
    </Paper>
  );
});

export default OccurrenceStats;
