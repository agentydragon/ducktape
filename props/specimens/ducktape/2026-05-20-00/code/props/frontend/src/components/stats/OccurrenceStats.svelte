<script lang="ts">
  interface OccurrenceStatsRow {
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

  let { occurrences }: Props = $props();

  function pctParts(v: number): { integer: string; fraction: string } {
    const [integer, fraction] = (v * 100).toFixed(1).split(".");
    return { integer, fraction: "." + fraction + "%" };
  }

  // Index by (tp_id, occurrence_id) for quick lookup
  const byKey = $derived(new Map(occurrences.map((o) => [`${o.tp_id}:${o.occurrence_id}`, o])));

  // Expose lookup function
  export function getStats(tpId: string, occurrenceId: string): OccurrenceStatsRow | undefined {
    return byKey.get(`${tpId}:${occurrenceId}`);
  }
</script>

<!-- Standalone table view for all occurrences -->
<div class="bg-white dark:bg-gray-900 rounded-lg border dark:border-gray-700">
  <div class="px-4 py-3 border-b dark:border-gray-700">
    <h3 class="text-sm font-semibold">Occurrence Detection Statistics</h3>
    <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
      Mean credit per occurrence across all critic runs. Low values indicate hard-to-find issues.
    </p>
  </div>
  {#if occurrences.length === 0}
    <div class="p-4 text-sm text-gray-500 dark:text-gray-400">No occurrence statistics available</div>
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b dark:border-gray-700 bg-gray-50 dark:bg-gray-800">
            <th class="px-3 py-2 text-left font-medium text-gray-600 dark:text-gray-400">TP ID</th>
            <th class="px-3 py-2 text-left font-medium text-gray-600 dark:text-gray-400">Occurrence</th>
            <th class="px-3 py-2 text-right font-medium text-gray-600 dark:text-gray-400">Mean</th>
            <th class="px-3 py-2 text-right font-medium text-gray-600 dark:text-gray-400">Min</th>
            <th class="px-3 py-2 text-right font-medium text-gray-600 dark:text-gray-400">Max</th>
            <th class="px-3 py-2 text-right font-medium text-gray-600 dark:text-gray-400">Runs</th>
          </tr>
        </thead>
        <tbody>
          {#each occurrences as occ (`${occ.tp_id}:${occ.occurrence_id}`)}
            {@const mean = pctParts(occ.mean_credit)}
            {@const min = pctParts(occ.min_credit)}
            {@const max = pctParts(occ.max_credit)}
            <tr class="border-b dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800">
              <td class="px-3 py-2 font-mono text-xs">{occ.tp_id}</td>
              <td class="px-3 py-2 font-mono text-xs">{occ.occurrence_id}</td>
              <td class="px-3 py-2 font-mono text-xs tabular-nums"
                ><span class="inline-block w-[3ch] text-right">{mean.integer}</span>{mean.fraction}</td
              >
              <td class="px-3 py-2 font-mono text-xs tabular-nums"
                ><span class="inline-block w-[3ch] text-right">{min.integer}</span>{min.fraction}</td
              >
              <td class="px-3 py-2 font-mono text-xs tabular-nums"
                ><span class="inline-block w-[3ch] text-right">{max.integer}</span>{max.fraction}</td
              >
              <td class="px-3 py-2 text-right text-xs text-gray-600 dark:text-gray-400">{occ.n_runs}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>
