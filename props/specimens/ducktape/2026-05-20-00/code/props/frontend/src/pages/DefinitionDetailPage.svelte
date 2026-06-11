<script lang="ts">
  import DefinitionDetail from "$components/DefinitionDetail.svelte";
  import { fetchDefinitionDetail, type DefinitionDetailResponse } from "$lib/api/client";

  interface Props {
    definitionId: string;
  }
  let { definitionId }: Props = $props();

  let definition: DefinitionDetailResponse | null = $state(null);
  let loading = $state(true);
  let error: string | null = $state(null);

  async function loadData() {
    loading = true;
    error = null;
    try {
      definition = await fetchDefinitionDetail(definitionId);
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load definition";
    } finally {
      loading = false;
    }
  }

  // $effect runs immediately on mount and re-runs when definitionId changes
  $effect(() => {
    if (definitionId) {
      loadData();
    }
  });
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
{:else if definition}
  <DefinitionDetail data={definition} />
{/if}
