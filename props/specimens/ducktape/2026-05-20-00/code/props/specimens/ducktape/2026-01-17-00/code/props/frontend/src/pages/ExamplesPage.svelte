<script lang="ts">
  import { onMount } from "svelte";
  import { pathname } from "$lib/router";
  import ExampleDetail from "$components/ExampleDetail.svelte";
  import { fetchExampleDetail, type ExampleDetailResponse, type ExampleKind } from "$lib/api/client";

  interface Props {
    initialData?: ExampleDetailResponse;
  }

  let { initialData }: Props = $props();

  let example: ExampleDetailResponse | null = $state(initialData ?? null);
  let loading = $state(!initialData);
  let error: string | null = $state(null);

  // Parse query params from hash URL
  const queryParams = $derived.by(() => {
    const path = $pathname;
    const queryStart = path.indexOf("?");
    if (queryStart === -1) return new URLSearchParams();
    return new URLSearchParams(path.slice(queryStart + 1));
  });

  const snapshotSlug = $derived(queryParams.get("snapshot_slug") ?? "");
  const exampleKind = $derived((queryParams.get("example_kind") ?? "whole_snapshot") as ExampleKind);
  const filesHash = $derived(queryParams.get("files_hash"));

  async function loadData() {
    if (!snapshotSlug) {
      error = "Missing snapshot_slug parameter";
      loading = false;
      return;
    }

    loading = true;
    error = null;
    try {
      example = await fetchExampleDetail(snapshotSlug, exampleKind, filesHash);
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load example";
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    if (!initialData) {
      loadData();
    }
  });

  // Reload when params change
  $effect(() => {
    if (snapshotSlug) {
      loadData();
    }
  });
</script>

{#if loading}
  <div class="flex items-center justify-center py-12">
    <div class="text-gray-500">Loading...</div>
  </div>
{:else if error}
  <p class="text-gray-500">{error}</p>
{:else if example}
  <ExampleDetail data={example} />
{/if}
