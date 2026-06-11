<script lang="ts">
  import { searchParams } from "$lib/router";
  import ExampleDetail from "$components/ExampleDetail.svelte";
  import { fetchExampleDetail, type ExampleDetailResponse, type ExampleKind } from "$lib/api/client";

  interface Props {
    initialData?: ExampleDetailResponse;
  }

  let { initialData }: Props = $props();

  // svelte-ignore state_referenced_locally
  let example: ExampleDetailResponse | null = $state(initialData ?? null);
  // svelte-ignore state_referenced_locally
  let loading = $state(!initialData);
  let error: string | null = $state(null);

  const snapshotSlug = $derived($searchParams.get("snapshot_slug") ?? "");
  const exampleKind = $derived(($searchParams.get("example_kind") ?? "whole_snapshot") as ExampleKind);
  const filesHash = $derived($searchParams.get("files_hash"));

  async function loadData(slug: string, kind: ExampleKind, hash: string | null) {
    loading = true;
    error = null;
    try {
      example = await fetchExampleDetail(slug, kind, hash);
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load example";
    } finally {
      loading = false;
    }
  }

  // $effect runs immediately on mount and re-runs when any query param changes
  $effect(() => {
    if (snapshotSlug && !initialData) {
      loadData(snapshotSlug, exampleKind, filesHash);
    } else if (!snapshotSlug) {
      error = "Missing snapshot_slug parameter";
      loading = false;
    }
  });
</script>

{#if loading}
  <div class="flex items-center justify-center py-12">
    <div class="text-gray-500 dark:text-gray-400">Loading...</div>
  </div>
{:else if error}
  <p class="text-gray-500 dark:text-gray-400">{error}</p>
{:else if example}
  <ExampleDetail data={example} />
{/if}
