<script lang="ts">
  import { getContext } from "svelte";
  import { type DefinitionDetailResponse } from "../lib/api/client";
  import { formatStatsWithCI, formatAge } from "../lib/formatters";
  import { recallColorClass } from "../lib/colors";
  import RunsBrowser from "./RunsBrowser.svelte";
  import BackButton from "./BackButton.svelte";
  import Breadcrumb from "./Breadcrumb.svelte";
  import type { RunModalPrefill, Split, ExampleKind } from "../lib/types";

  const runModal = getContext<{ open: (_?: RunModalPrefill) => void }>("runModal");

  interface Props {
    data: DefinitionDetailResponse;
  }
  let { data }: Props = $props();

  // Column group configs (same as DefinitionsTable)
  const colGroups: { split: Split; kind: ExampleKind; label: string }[] = [
    { split: "valid", kind: "whole_snapshot", label: "Valid Whole" },
    { split: "valid", kind: "file_set", label: "Valid Partial" },
    { split: "train", kind: "whole_snapshot", label: "Train Whole" },
    { split: "train", kind: "file_set", label: "Train Partial" },
  ];

  function getStats(split: Split, kind: ExampleKind) {
    return data.stats[split]?.[kind];
  }
</script>

<div class="space-y-4">
  <!-- Header -->
  <div class="bg-white dark:bg-gray-900 rounded-lg shadow dark:shadow-gray-950/30 p-4">
    <div class="flex items-center gap-3 mb-3">
      <BackButton />
      <h2 class="text-lg font-semibold">Definition Detail</h2>
      <div class="ml-auto">
        <button
          type="button"
          class="px-3 py-1 text-sm bg-blue-500 text-white rounded hover:bg-blue-600"
          onclick={() => runModal?.open({ definitionId: data.image_digest })}
        >
          + New Run
        </button>
      </div>
    </div>
    <Breadcrumb
      items={[
        { label: "Home", href: "/" },
        { label: "Definitions", href: "/" },
        { label: data.display_name ?? data.image_digest },
      ]}
    />

    <div class="space-y-3 mt-3">
      <!-- Definition ID and metadata -->
      <div class="flex items-center gap-4 text-sm">
        {#if data.display_name}
          <span class="font-semibold text-gray-800 dark:text-gray-200">{data.display_name}</span>
          <span class="text-gray-400 dark:text-gray-500">|</span>
        {/if}
        <span class="font-mono text-blue-600 dark:text-blue-400">{data.image_digest}</span>
        <span class="text-gray-400 dark:text-gray-500">|</span>
        <span class="text-gray-600 dark:text-gray-400">{data.agent_type}</span>
        <span class="text-gray-400 dark:text-gray-500">|</span>
        <span class="text-gray-600 dark:text-gray-400">{formatAge(data.created_at)}</span>
      </div>
    </div>
  </div>

  <!-- Stats table -->
  <div class="bg-white dark:bg-gray-900 rounded-lg shadow dark:shadow-gray-950/30 p-4">
    <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Recall by Split/Kind</h3>
    <div class="overflow-x-auto">
      <table class="min-w-full text-sm">
        <thead>
          <tr class="border-b border-gray-300 dark:border-gray-600">
            <th class="px-3 py-2 text-left">Split/Kind</th>
            <th class="px-3 py-2 text-right">Recall</th>
            <th class="px-3 py-2 text-right">N</th>
            <th class="px-3 py-2 text-right">Zero</th>
            <th class="px-3 py-2 text-right">Completed</th>
            <th class="px-3 py-2 text-right">Max Turns</th>
          </tr>
        </thead>
        <tbody>
          {#each colGroups as { split, kind, label } (`${split}-${kind}`)}
            {@const stats = getStats(split, kind)}
            <tr class="border-b border-gray-100 dark:border-gray-800">
              <td class="px-3 py-2 font-medium">{label}</td>
              {#if stats}
                <td class="px-3 py-2 text-right {recallColorClass(stats.recall_stats?.mean)}">
                  {stats.recall_stats ? formatStatsWithCI(stats.recall_stats) : "—"}
                </td>
                <td class="px-3 py-2 text-right">
                  {stats.n_examples}/{stats.total_available}
                </td>
                <td class="px-3 py-2 text-right text-gray-400 dark:text-gray-500">{stats.zero_count}</td>
                <td class="px-3 py-2 text-right">{stats.status_counts?.completed ?? 0}</td>
                <td class="px-3 py-2 text-right text-gray-400 dark:text-gray-500"
                  >{stats.status_counts?.timed_out ?? 0}</td
                >
              {:else}
                <td class="px-3 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
                <td class="px-3 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
                <td class="px-3 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
                <td class="px-3 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
                <td class="px-3 py-2 text-right text-gray-300 dark:text-gray-600">—</td>
              {/if}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Runs for this definition -->
  <RunsBrowser initialDefinitionId={data.image_digest} />
</div>
