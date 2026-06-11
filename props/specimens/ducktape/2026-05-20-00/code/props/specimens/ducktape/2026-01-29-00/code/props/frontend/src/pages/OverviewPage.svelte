<script lang="ts">
  import { onMount, getContext } from "svelte";
  import { goto, resolve } from "$lib/router";
  import type { RunModalPrefill } from "$lib/types";
  import { fetchOverview, type OverviewResponse } from "$lib/api/client";
  import DefinitionsTable from "$components/stats/DefinitionsTable.svelte";
  import SummaryCards from "$components/stats/SummaryCards.svelte";
  import JobsList from "$components/JobsList.svelte";
  import RunList from "$components/RunList.svelte";

  interface Props {
    initialData?: OverviewResponse;
  }

  let { initialData }: Props = $props();

  const runModal = getContext<{
    open: (_?: RunModalPrefill) => void;
  }>("runModal");

  let overview: OverviewResponse | null = $state(initialData ?? null);
  let loading = $state(!initialData);
  let error: string | null = $state(null);

  async function loadData() {
    loading = true;
    error = null;
    try {
      overview = await fetchOverview();
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load overview";
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    if (!initialData) {
      loadData();
    }
  });

  function handleNavigateToRuns(filters: RunModalPrefill) {
    const params = new URLSearchParams();
    if (filters.definitionId) params.set("definition", filters.definitionId);
    if (filters.split) params.set("split", filters.split);
    if (filters.kind) params.set("kind", filters.kind);
    const qs = params.toString();
    goto(qs ? `/runs?${qs}` : "/runs");
  }
</script>

{#if loading}
  <div class="flex items-center justify-center py-12">
    <div class="text-gray-500">Loading...</div>
  </div>
{:else if error}
  <div class="bg-red-50 border border-red-200 rounded p-4 text-red-700">
    {error}
  </div>
{:else if overview}
  <div>
    <JobsList onNewRun={() => runModal?.open()} />

    <div class="mb-4">
      <RunList />
    </div>

    <SummaryCards data={overview} />
    <div class="bg-white rounded-lg shadow">
      <DefinitionsTable
        definitions={overview.definitions}
        exampleCounts={overview.example_counts}
        onCellClick={handleNavigateToRuns}
      />
    </div>
  </div>
{/if}
