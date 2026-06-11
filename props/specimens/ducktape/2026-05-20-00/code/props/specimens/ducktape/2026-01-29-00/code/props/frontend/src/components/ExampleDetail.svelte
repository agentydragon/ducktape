<script lang="ts">
  import { type ExampleDetailResponse } from "../lib/api/client";
  import { formatStatsWithCI } from "../lib/formatters";
  import { recallColorClass } from "../lib/colors";
  import DefinitionIdLink from "../lib/DefinitionIdLink.svelte";
  import SnapshotLink from "../lib/SnapshotLink.svelte";
  import FileLink from "../lib/FileLink.svelte";
  import BackButton from "./BackButton.svelte";
  import Breadcrumb from "./Breadcrumb.svelte";

  interface Props {
    data: ExampleDetailResponse;
  }
  let { data }: Props = $props();

  function formatStatusCounts(counts: Record<string, number>): string {
    const parts: string[] = [];
    if (counts.completed) parts.push(`${counts.completed} completed`);
    if (counts.max_turns_exceeded) parts.push(`${counts.max_turns_exceeded} max_turns`);
    if (counts.in_progress) parts.push(`${counts.in_progress} in_progress`);
    return parts.join(", ") || "—";
  }
</script>

<div class="space-y-4">
  <!-- Header -->
  <div class="bg-white rounded-lg shadow p-4">
    <div class="flex items-center gap-3 mb-3">
      <BackButton />
      <h2 class="text-lg font-semibold">Example Detail</h2>
    </div>
    <Breadcrumb
      items={[
        { label: "Home", href: "/" },
        { label: "Examples", href: "/examples" },
        {
          label: `${data.snapshot_slug}/${data.example_kind}${data.files_hash ? `/${data.files_hash.substring(0, 8)}` : ""}`,
        },
      ]}
    />

    <div class="space-y-3">
      <!-- Example metadata -->
      <div class="grid grid-cols-2 gap-4 text-sm">
        <div>
          <span class="text-gray-500">Snapshot:</span>
          <span class="ml-1">
            <SnapshotLink slug={data.snapshot_slug} />
          </span>
        </div>
        <div>
          <span class="text-gray-500">Split:</span>
          <span class="ml-1 capitalize">{data.split}</span>
        </div>
        <div>
          <span class="text-gray-500">Kind:</span>
          <span class="ml-1">{data.example_kind}</span>
        </div>
        <div>
          <span class="text-gray-500">Catchable Occurrences:</span>
          <span class="ml-1">{data.recall_denominator}</span>
        </div>
        {#if data.files_hash}
          <div class="col-span-2">
            <span class="text-gray-500">Files Hash:</span>
            <span class="ml-1 font-mono text-xs">{data.files_hash}</span>
          </div>
        {/if}
      </div>

      <!-- File list for file_set examples -->
      {#if data.example_kind === "file_set" && data.files}
        <div>
          <h3 class="text-sm font-medium text-gray-700 mb-2">Files ({data.files.length})</h3>
          <ul class="text-xs text-gray-600 space-y-1">
            {#each data.files as file (file)}
              <li>
                <FileLink snapshotSlug={data.snapshot_slug} filePath={file} />
              </li>
            {/each}
          </ul>
        </div>
      {/if}

      <!-- Aggregate stats -->
      {#if data.credit_stats}
        <div class="pt-3 border-t">
          <span class="text-sm text-gray-500">Aggregate Recall:</span>
          <span class="ml-2 text-sm {recallColorClass(data.credit_stats.mean)}">
            {formatStatsWithCI(data.credit_stats)}
          </span>
        </div>
      {/if}
    </div>
  </div>

  <!-- Definition stats table -->
  {#if data.definitions.length > 0}
    <div class="bg-white rounded-lg shadow p-4">
      <h3 class="text-sm font-medium text-gray-700 mb-3">Definitions ({data.definitions.length})</h3>
      <div class="overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead>
            <tr class="border-b border-gray-300">
              <th class="px-3 py-2 text-left">Definition</th>
              <th class="px-3 py-2 text-left">Model</th>
              <th class="px-3 py-2 text-right">Recall</th>
              <th class="px-3 py-2 text-right">N Runs</th>
              <th class="px-3 py-2 text-left">Status</th>
            </tr>
          </thead>
          <tbody>
            {#each data.definitions as def (def.image_digest)}
              <tr class="border-b border-gray-100 hover:bg-gray-50">
                <td class="px-3 py-2">
                  <DefinitionIdLink id={def.image_digest} />
                </td>
                <td class="px-3 py-2 text-gray-600 font-mono text-xs">{def.model}</td>
                <td class="px-3 py-2 text-right {recallColorClass(def.credit_stats?.mean)}">
                  {def.credit_stats ? formatStatsWithCI(def.credit_stats) : "—"}
                </td>
                <td class="px-3 py-2 text-right text-gray-600">{def.n_runs}</td>
                <td class="px-3 py-2 text-xs text-gray-600">{formatStatusCounts(def.status_counts)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {:else}
    <div class="bg-white rounded-lg shadow p-4">
      <p class="text-gray-500 text-sm">No definitions have been evaluated on this example yet.</p>
    </div>
  {/if}
</div>
