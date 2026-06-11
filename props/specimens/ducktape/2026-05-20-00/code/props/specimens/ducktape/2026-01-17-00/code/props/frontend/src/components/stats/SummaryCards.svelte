<script lang="ts">
  import type { OverviewResponse, DefinitionRow, StatsWithCI } from "../../lib/types";
  import { formatStatsWithCI } from "../../lib/formatters";
  import DefinitionIdLink from "../../lib/DefinitionIdLink.svelte";

  interface Props {
    data: OverviewResponse;
  }

  let { data }: Props = $props();

  // Find best definition by valid whole-snapshot recall (preserves full StatsWithCI)
  function findBestDefinition(): { def: DefinitionRow; recall_stats: StatsWithCI } | null {
    let best: { def: DefinitionRow; recall_stats: StatsWithCI } | null = null;
    for (const def of data.definitions) {
      const stats = def.stats["valid"]?.["whole_snapshot"];
      const recall_stats = stats?.recall_stats;
      if (recall_stats != null) {
        if (!best || recall_stats.mean > best.recall_stats.mean) {
          best = { def, recall_stats };
        }
      }
    }
    return best;
  }

  // Aggregate status counts across all definitions
  function aggregateStatusCounts(): Record<string, number> {
    const counts: Record<string, number> = {};
    for (const def of data.definitions) {
      for (const splitStats of Object.values(def.stats)) {
        for (const kindStats of Object.values(splitStats)) {
          if (kindStats?.status_counts) {
            for (const [status, count] of Object.entries(kindStats.status_counts)) {
              counts[status] = (counts[status] ?? 0) + (count ?? 0);
            }
          }
        }
      }
    }
    return counts;
  }

  const bestDef = $derived(findBestDefinition());
  const statusCounts = $derived(aggregateStatusCounts());
  const totalRuns = $derived(Object.values(statusCounts).reduce((a, b) => a + b, 0));
</script>

<div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
  <!-- Definitions count -->
  <div class="bg-white rounded-lg shadow p-4">
    <h3 class="text-sm font-medium text-gray-500 mb-2">Definitions</h3>
    <div class="text-2xl font-bold">{data.total_definitions}</div>
    <div class="text-xs text-gray-500 mt-1">Critic definitions</div>
  </div>

  <!-- Best definition -->
  <div class="bg-white rounded-lg shadow p-4">
    <h3 class="text-sm font-medium text-gray-500 mb-2">Best (Valid Whole)</h3>
    {#if bestDef}
      <div class="text-2xl font-bold text-green-600">{formatStatsWithCI(bestDef.recall_stats)}</div>
      <div class="text-xs mt-1">
        <DefinitionIdLink id={bestDef.def.image_digest} />
      </div>
    {:else}
      <div class="text-2xl font-bold text-gray-400">-</div>
      <div class="text-xs text-gray-500 mt-1">No valid runs yet</div>
    {/if}
  </div>

  <!-- Run status breakdown -->
  <div class="bg-white rounded-lg shadow p-4">
    <h3 class="text-sm font-medium text-gray-500 mb-2">Runs ({totalRuns})</h3>
    <div class="flex gap-2 text-xs">
      {#if statusCounts["completed"]}
        <span class="text-green-600" title="Completed">
          ✓{statusCounts["completed"]}
        </span>
      {/if}
      {#if statusCounts["in_progress"]}
        <span class="text-blue-600" title="In Progress">
          ⟳{statusCounts["in_progress"]}
        </span>
      {/if}
      {#if statusCounts["max_turns_exceeded"]}
        <span class="text-yellow-600" title="Max Turns Exceeded">
          S{statusCounts["max_turns_exceeded"]}
        </span>
      {/if}
      {#if statusCounts["context_length_exceeded"]}
        <span class="text-orange-600" title="Context Exceeded">
          C{statusCounts["context_length_exceeded"]}
        </span>
      {/if}
      {#if statusCounts["reported_failure"]}
        <span class="text-red-600" title="Failed">
          F{statusCounts["reported_failure"]}
        </span>
      {/if}
    </div>
  </div>
</div>
