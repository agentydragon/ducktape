<script lang="ts">
  import { type DefinitionDetailResponse } from "../lib/api/client";
  import { formatStatsWithCI, formatAge } from "../lib/formatters";
  import { recallColorClass } from "../lib/colors";
  import RunsBrowser from "./RunsBrowser.svelte";
  import BackButton from "./BackButton.svelte";
  import Breadcrumb from "./Breadcrumb.svelte";
  import type { Split, ExampleKind } from "../lib/types";

  interface Props {
    data: DefinitionDetailResponse;
  }
  let { data }: Props = $props();

  let copied = $state(false);

  const cliCommand = $derived(`props agent-pkg fetch ${data.image_digest} /workspace/my_pkg/`);

  async function copyCommand() {
    await navigator.clipboard.writeText(cliCommand);
    copied = true;
    setTimeout(() => (copied = false), 2000);
  }

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
  <div class="bg-white rounded-lg shadow p-4">
    <div class="flex items-center gap-3 mb-3">
      <BackButton />
      <h2 class="text-lg font-semibold">Definition Detail</h2>
    </div>
    <Breadcrumb
      items={[{ label: "Home", href: "/" }, { label: "Definitions", href: "/" }, { label: data.image_digest }]}
    />

    <div class="space-y-3 mt-3">
      <!-- Definition ID and metadata -->
      <div class="flex items-center gap-4 text-sm">
        <span class="font-mono text-blue-600">{data.image_digest}</span>
        <span class="text-gray-400">|</span>
        <span class="text-gray-600">{data.agent_type}</span>
        <span class="text-gray-400">|</span>
        <span class="text-gray-600">{formatAge(data.created_at)}</span>
      </div>

      <!-- CLI command -->
      <div class="flex items-center gap-2">
        <code class="flex-1 bg-gray-100 px-3 py-2 rounded text-sm font-mono text-gray-800 overflow-x-auto">
          {cliCommand}
        </code>
        <button
          type="button"
          class="px-3 py-2 text-sm border rounded hover:bg-gray-50 whitespace-nowrap"
          onclick={copyCommand}
        >
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
    </div>
  </div>

  <!-- Stats table -->
  <div class="bg-white rounded-lg shadow p-4">
    <h3 class="text-sm font-medium text-gray-700 mb-3">Recall by Split/Kind</h3>
    <div class="overflow-x-auto">
      <table class="min-w-full text-sm">
        <thead>
          <tr class="border-b border-gray-300">
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
            <tr class="border-b border-gray-100">
              <td class="px-3 py-2 font-medium">{label}</td>
              {#if stats}
                <td class="px-3 py-2 text-right {recallColorClass(stats.recall_stats?.mean)}">
                  {stats.recall_stats ? formatStatsWithCI(stats.recall_stats) : "—"}
                </td>
                <td class="px-3 py-2 text-right">
                  {stats.n_examples}/{stats.total_available}
                </td>
                <td class="px-3 py-2 text-right text-gray-400">{stats.zero_count}</td>
                <td class="px-3 py-2 text-right">{stats.status_counts?.completed ?? 0}</td>
                <td class="px-3 py-2 text-right text-gray-400">{stats.status_counts?.max_turns_exceeded ?? 0}</td>
              {:else}
                <td class="px-3 py-2 text-right text-gray-300">—</td>
                <td class="px-3 py-2 text-right text-gray-300">—</td>
                <td class="px-3 py-2 text-right text-gray-300">—</td>
                <td class="px-3 py-2 text-right text-gray-300">—</td>
                <td class="px-3 py-2 text-right text-gray-300">—</td>
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
