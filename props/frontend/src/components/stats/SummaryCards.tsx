import { Card, SimpleGrid, Text } from "@mantine/core";
import type { OverviewResponse, DefinitionRow, StatsWithCI } from "../../lib/types";
import { formatStatsWithCI } from "../../lib/formatters";
import DefinitionIdLink from "../../lib/DefinitionIdLink";

interface Props {
  data: OverviewResponse;
}

export default function SummaryCards({ data }: Props) {
  let bestDefinition: { definition: DefinitionRow; recallStats: StatsWithCI } | null = null;
  for (const definition of data.definitions) {
    const stats = definition.stats["valid"]?.["whole_snapshot"];
    const recallStats = stats?.recall_stats;
    if (recallStats != null && (!bestDefinition || recallStats.mean > bestDefinition.recallStats.mean)) {
      bestDefinition = { definition, recallStats };
    }
  }

  const statusCounts: Record<string, number> = {};
  for (const definition of data.definitions) {
    for (const splitStats of Object.values(definition.stats)) {
      for (const kindStats of Object.values(splitStats)) {
        if (kindStats?.status_counts) {
          for (const [status, count] of Object.entries(kindStats.status_counts)) {
            statusCounts[status] = (statusCounts[status] ?? 0) + (count ?? 0);
          }
        }
      }
    }
  }
  const totalRuns = Object.values(statusCounts).reduce((sum, count) => sum + count, 0);

  return (
    <SimpleGrid cols={{ base: 1, md: 3 }} spacing="md" mb="lg">
      <Card shadow="sm" padding="md" radius="md" className="bg-white dark:bg-gray-900 dark:shadow-gray-950/30">
        <Text size="sm" fw={500} c="dimmed" mb="xs">
          Definitions
        </Text>
        <Text size="xl" fw={700}>
          {data.definitions.length}
        </Text>
        <Text size="xs" c="dimmed" mt={4}>
          Critic definitions
        </Text>
      </Card>

      <Card shadow="sm" padding="md" radius="md" className="bg-white dark:bg-gray-900 dark:shadow-gray-950/30">
        <Text size="sm" fw={500} c="dimmed" mb="xs">
          Best (Valid Whole)
        </Text>
        {bestDefinition ? (
          <>
            <Text size="xl" fw={700} c="green">
              {formatStatsWithCI(bestDefinition.recallStats)}
            </Text>
            <Text size="xs" mt={4}>
              <DefinitionIdLink id={bestDefinition.definition.image_digest} />
            </Text>
          </>
        ) : (
          <>
            <Text size="xl" fw={700} c="dimmed">
              -
            </Text>
            <Text size="xs" c="dimmed" mt={4}>
              No valid runs yet
            </Text>
          </>
        )}
      </Card>

      <Card shadow="sm" padding="md" radius="md" className="bg-white dark:bg-gray-900 dark:shadow-gray-950/30">
        <Text size="sm" fw={500} c="dimmed" mb="xs">
          Runs ({totalRuns})
        </Text>
        <div className="flex gap-2 text-xs">
          {statusCounts["exited"] ? (
            <span className="text-green-600 dark:text-green-400" title="Exited">
              ✓{statusCounts["exited"]}
            </span>
          ) : null}
          {statusCounts["in_progress"] ? (
            <span className="text-blue-600 dark:text-blue-400" title="In Progress">
              ⟳{statusCounts["in_progress"]}
            </span>
          ) : null}
          {statusCounts["timed_out"] ? (
            <span className="text-yellow-600 dark:text-yellow-400" title="Timed Out">
              T{statusCounts["timed_out"]}
            </span>
          ) : null}
        </div>
      </Card>
    </SimpleGrid>
  );
}
