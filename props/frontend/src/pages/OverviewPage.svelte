<script lang="ts">
  import { onMount, getContext } from "svelte";
  import { goto } from "$lib/router";
  import type { RunModalPrefill } from "$lib/types";
  import { fetchOverview, fetchCoverage, type OverviewResponse, type CoverageResponse } from "$lib/api/client";
  import DefinitionsTable from "$components/stats/DefinitionsTable.svelte";
  import SummaryCards from "$components/stats/SummaryCards.svelte";
  import DistributionChart from "$components/stats/DistributionChart.svelte";
  import CoverageHeatmap from "$components/stats/CoverageHeatmap.svelte";
  import TabButton from "$components/TabButton.svelte";
  import { toast } from "svelte-sonner";

  const runModal = getContext<{ open: (_?: RunModalPrefill) => void }>("runModal");

  interface Props {
    initialData?: OverviewResponse;
  }

  let { initialData }: Props = $props();

  // svelte-ignore state_referenced_locally
  let overview: OverviewResponse | null = $state(initialData ?? null);
  // svelte-ignore state_referenced_locally
  let loading = $state(!initialData);
  let error: string | null = $state(null);

  let analysisSplit: "valid" | "train" = $state("valid");
  let coverage: CoverageResponse | null = $state(null);
  let analysisLoading = $state(false);

  let analysisRequestId = 0;

  async function loadAnalysis(split: "valid" | "train") {
    const requestId = ++analysisRequestId;
    analysisLoading = true;
    try {
      const cov = await fetchCoverage(split);
      // Guard against stale responses from rapid split switching
      if (requestId === analysisRequestId) {
        coverage = cov;
      }
    } catch (e) {
      if (requestId === analysisRequestId) {
        toast.error(e instanceof Error ? e.message : "Failed to load analysis");
      }
    } finally {
      if (requestId === analysisRequestId) {
        analysisLoading = false;
      }
    }
  }

  $effect(() => {
    if (overview) {
      loadAnalysis(analysisSplit);
    }
  });

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
    <div class="text-gray-500 dark:text-gray-400">Loading...</div>
  </div>
{:else if error}
  <div
    class="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 rounded p-4 text-red-700 dark:text-red-300"
  >
    {error}
  </div>
{:else if overview}
  <div>
    <SummaryCards data={overview} />
    <div class="flex justify-end mb-3">
      <button
        type="button"
        class="px-3 py-1 text-sm bg-blue-500 text-white rounded hover:bg-blue-600"
        onclick={() => runModal?.open()}
      >
        + New Run
      </button>
    </div>
    <div class="bg-white dark:bg-gray-900 rounded-lg shadow dark:shadow-gray-950/30">
      <DefinitionsTable
        definitions={overview.definitions}
        exampleCounts={overview.example_counts}
        onCellClick={handleNavigateToRuns}
      />
    </div>

    <!-- Analysis Section -->
    <div class="mt-4 bg-white dark:bg-gray-900 rounded-lg shadow dark:shadow-gray-950/30 p-4">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold">Stats & Analysis</h3>
        <div class="flex border rounded dark:border-gray-700">
          <TabButton active={analysisSplit === "valid"} onclick={() => (analysisSplit = "valid")}>Valid</TabButton>
          <TabButton active={analysisSplit === "train"} onclick={() => (analysisSplit = "train")}>Train</TabButton>
        </div>
      </div>

      {#if analysisLoading}
        <div class="text-gray-500 dark:text-gray-400 text-center py-8">Loading analysis...</div>
      {:else if coverage}
        <div class="grid grid-cols-2 gap-4 mb-4">
          <DistributionChart
            values={coverage.max_recall_values}
            title="Max Recall Distribution"
            numBuckets={10}
            valueFormat={(v) => `${(v * 100).toFixed(0)}%`}
            color="rgb(59, 130, 246)"
          />
          <DistributionChart
            values={coverage.tp_count_values}
            title="TP Occurrence Count Distribution"
            numBuckets={8}
            valueFormat={(v) => `${v.toFixed(0)}`}
            color="rgb(34, 197, 94)"
          />
        </div>

        {#if coverage.definitions.length > 0}
          <CoverageHeatmap definitions={coverage.definitions} examples={coverage.examples} cells={coverage.cells} />
        {/if}
      {/if}
    </div>
  </div>
{/if}
