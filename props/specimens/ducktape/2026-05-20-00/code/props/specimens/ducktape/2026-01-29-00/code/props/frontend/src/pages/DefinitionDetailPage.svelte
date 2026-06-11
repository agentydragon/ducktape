<script lang="ts">
  import { onMount } from "svelte";
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

  onMount(() => {
    loadData();
  });

  // Reload when definitionId changes
  $effect(() => {
    if (definitionId) {
      loadData();
    }
  });
</script>

{#if loading}
  <div class="flex items-center justify-center py-12">
    <div class="text-gray-500">Loading...</div>
  </div>
{:else if error}
  <div class="bg-red-50 border border-red-200 rounded p-4 text-red-700">
    {error}
  </div>
{:else if definition}
  <DefinitionDetail data={definition} />
{/if}
