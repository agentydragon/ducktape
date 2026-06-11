<script lang="ts">
  import { DataTable } from "@careswitch/svelte-data-table";
  import type { DefinitionRow, SplitScopeStats, Split, ExampleKind } from "../../lib/types";
  import { formatStatsWithCI, formatAge } from "../../lib/formatters";
  import { recallColorClass } from "../../lib/colors";
  import DefinitionIdLink from "../../lib/DefinitionIdLink.svelte";

  interface CellClickInfo {
    definitionId: string;
    split: Split;
    kind: ExampleKind;
  }

  interface Props {
    definitions: DefinitionRow[];
    exampleCounts?: { [key: string]: { [key: string]: number } };
    onCellClick?: (_: CellClickInfo) => void;
  }

  let { definitions, exampleCounts, onCellClick }: Props = $props();

  function getStats(def: DefinitionRow, split: Split, kind: ExampleKind): SplitScopeStats | undefined {
    return def.stats[split]?.[kind];
  }

  // Use formatAge from formatters.ts with addSuffix=false for table display
  const formatTableAge = (isoDate: string) => formatAge(isoDate, false);

  function getExampleCount(split: Split, kind: ExampleKind): number {
    return exampleCounts?.[split]?.[kind] ?? 0;
  }

  // Hierarchical structure: splits -> kinds
  const splits: Split[] = ["valid", "train"];
  const kinds: ExampleKind[] = ["whole_snapshot", "file_set"];
  const metricsPerKind = 5; // Recall, Runs, Zero, Done, Stalled

  // Sort state tracked outside DataTable so it survives data changes
  let sortColumn = $state("valid_whole_snapshot_recall");
  let sortDirection: "asc" | "desc" = $state("desc");

  const table = $derived(
    new DataTable({
      data: definitions,
      columns: [
        { id: "image_digest", key: "image_digest", name: "Definition", sortable: true },
        { id: "created_at", key: "created_at", name: "Age", sortable: true },
        // Generate columns for each split/kind combo
        ...splits.flatMap((split) =>
          kinds.map((kind) => ({
            id: `${split}_${kind}_recall`,
            key: "stats" as keyof DefinitionRow,
            name: `${split} ${kind} Recall`,
            sortable: true,
            getValue: (row: DefinitionRow) => getStats(row, split, kind)?.recall_stats?.mean ?? -1,
          }))
        ),
      ],
      initialSort: sortColumn,
      initialSortDirection: sortDirection,
    })
  );

  function handleSort(columnId: string) {
    if (sortColumn === columnId) {
      sortDirection = sortDirection === "asc" ? "desc" : "asc";
    } else {
      sortColumn = columnId;
      sortDirection = "asc";
    }
  }

  function getSortIndicator(columnId: string): string {
    if (sortColumn !== columnId) return "";
    return sortDirection === "asc" ? " ↑" : " ↓";
  }
</script>

<div class="overflow-x-auto">
  <table class="min-w-full text-sm">
    <thead>
      <!-- Level 1: Split (Valid / Train) -->
      <tr class="border-b border-gray-200 dark:border-gray-700">
        <th
          rowspan="3"
          class="px-3 py-2 text-left cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 align-bottom"
          onclick={() => handleSort("image_digest")}
        >
          Definition{getSortIndicator("image_digest")}
        </th>
        <th
          rowspan="3"
          class="px-3 py-2 text-right cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 align-bottom"
          onclick={() => handleSort("created_at")}
        >
          Age{getSortIndicator("created_at")}
        </th>
        {#each splits as split (split)}
          <th
            colspan={kinds.length * metricsPerKind}
            class="px-3 py-1 text-center border-l border-gray-300 dark:border-gray-600 font-semibold capitalize"
          >
            {split}
          </th>
        {/each}
      </tr>
      <!-- Level 2: Kind with example counts -->
      <tr class="border-b border-gray-200 dark:border-gray-700">
        {#each splits as split (split)}
          {#each kinds as kind (`${split}-${kind}`)}
            {@const colId = `${split}_${kind}_recall`}
            {@const count = getExampleCount(split, kind)}
            <th
              colspan={metricsPerKind}
              class="px-2 py-1 text-center border-l border-gray-200 dark:border-gray-700 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700"
              onclick={() => handleSort(colId)}
            >
              {kind} <span class="text-gray-400 dark:text-gray-500 font-normal">(n={count})</span>{getSortIndicator(
                colId
              )}
            </th>
          {/each}
        {/each}
      </tr>
      <!-- Level 3: Metrics -->
      <tr class="border-b border-gray-300 dark:border-gray-600 text-xs text-gray-500 dark:text-gray-400">
        {#each splits as _split (_split)}
          {#each kinds as _kind (`${_split}-${_kind}`)}
            <th class="px-2 py-1 text-right border-l border-gray-200 dark:border-gray-700">Recall</th>
            <th class="px-2 py-1 text-right">Runs</th>
            <th class="px-2 py-1 text-right">Zero</th>
            <th class="px-2 py-1 text-right">Done</th>
            <th class="px-2 py-1 text-right">Stalled</th>
          {/each}
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each table.rows as def (def.image_digest)}
        <tr class="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800">
          <td class="px-3 py-2 font-mono text-xs">
            <DefinitionIdLink id={def.image_digest} display_name={def.display_name} />
          </td>
          <td class="px-3 py-2 text-right text-gray-600 dark:text-gray-400">
            {formatTableAge(def.created_at)}
          </td>
          {#each splits as split (split)}
            {#each kinds as kind (`${split}-${kind}`)}
              {@const stats = getStats(def, split, kind)}
              {@const clickable = onCellClick != null}
              {@const cellClick = clickable
                ? () => onCellClick({ definitionId: def.image_digest, split, kind })
                : undefined}
              {@const clickClass = clickable ? "cursor-pointer hover:bg-blue-100 dark:hover:bg-blue-900" : ""}
              {@const clickTitle = clickable ? `View ${split} ${kind} runs` : undefined}
              {#if stats}
                <td
                  class="px-2 py-2 text-right border-l border-gray-100 dark:border-gray-800 {recallColorClass(
                    stats.recall_stats?.mean
                  )} {clickClass}"
                  onclick={cellClick}
                  title={clickTitle}
                >
                  {stats.recall_stats ? formatStatsWithCI(stats.recall_stats) : "—"}
                </td>
                <td class="px-2 py-2 text-right">{stats.n_examples}</td>
                <td class="px-2 py-2 text-right text-gray-400 dark:text-gray-500">{stats.zero_count}</td>
                <td class="px-2 py-2 text-right">{stats.status_counts.completed ?? 0}</td>
                <td class="px-2 py-2 text-right text-gray-400 dark:text-gray-500"
                  >{stats.status_counts.timed_out ?? 0}</td
                >
              {:else}
                <td
                  class="px-2 py-2 text-right border-l border-gray-100 dark:border-gray-800 text-gray-300 dark:text-gray-600 {clickClass}"
                  onclick={cellClick}
                  title={clickTitle}>—</td
                >
                <td class="px-2 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
                <td class="px-2 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
                <td class="px-2 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
                <td class="px-2 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
              {/if}
            {/each}
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
</div>
