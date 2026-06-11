<script lang="ts">
  import { runs } from "$lib/stores/runsFeed";
  import { getStatusColor, formatStatus } from "$lib/status";
  import DefinitionIdLink from "$lib/DefinitionIdLink.svelte";
  import RunIdLink from "$lib/RunIdLink.svelte";

  // Filter to show only in-progress runs
  const activeRuns = $derived($runs.filter((r) => r.status === "in_progress"));
</script>

<div class="bg-white rounded-lg shadow p-4">
  <h2 class="text-lg font-semibold mb-3">Active Runs</h2>

  {#if activeRuns.length === 0}
    <p class="text-gray-500 text-sm">No active runs</p>
  {:else}
    <div class="space-y-2">
      {#each activeRuns as run (run.agent_run_id)}
        <div class="block w-full text-left p-3 rounded border hover:bg-gray-50 transition-colors">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-3">
              <RunIdLink id={run.agent_run_id} />
              <DefinitionIdLink id={run.image_digest} />
            </div>
            <div class="flex items-center gap-2">
              <span class="text-xs text-gray-500">{run.model}</span>
              <span class="px-2 py-0.5 rounded text-xs font-medium capitalize {getStatusColor(run.status)}">
                {formatStatus(run.status)}
              </span>
            </div>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>
