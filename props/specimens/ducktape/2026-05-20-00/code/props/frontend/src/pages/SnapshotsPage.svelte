<script lang="ts">
  import { onMount } from "svelte";
  import { goto, resolve } from "$lib/router";
  import { fetchSnapshots, type SnapshotsResponse } from "$lib/api/client";
  import { splitBadgeClass } from "$lib/colors";

  interface Props {
    initialData?: SnapshotsResponse["snapshots"];
  }

  let { initialData }: Props = $props();

  // svelte-ignore state_referenced_locally
  let snapshots: SnapshotsResponse["snapshots"] = $state(initialData ?? []);
  // svelte-ignore state_referenced_locally
  let loading = $state(!initialData);
  let error: string | null = $state(null);

  async function loadData() {
    loading = true;
    error = null;
    try {
      const data = await fetchSnapshots();
      snapshots = data.snapshots;
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load snapshots";
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    if (!initialData) {
      loadData();
    }
  });

  function formatDate(dateStr: string): string {
    return new Date(dateStr).toLocaleDateString();
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
{:else}
  <div class="bg-white dark:bg-gray-900 rounded-lg shadow dark:shadow-gray-950/30 p-4">
    <h2 class="text-xl font-semibold mb-4">Snapshots</h2>

    {#if snapshots.length === 0}
      <p class="text-gray-500 dark:text-gray-400">No snapshots found</p>
    {:else}
      <div class="overflow-x-auto">
        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
          <thead class="bg-gray-50 dark:bg-gray-800">
            <tr>
              <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Slug</th>
              <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Split</th>
              <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">TPs</th>
              <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">FPs</th>
              <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase"
                >Created</th
              >
            </tr>
          </thead>
          <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-700">
            {#each snapshots as snapshot (snapshot.slug)}
              <tr
                class="hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer"
                onclick={() => goto(`/snapshots/${snapshot.slug}`)}
              >
                <td class="px-4 py-2 font-mono text-sm">{snapshot.slug}</td>
                <td class="px-4 py-2">
                  <span class="px-2 py-1 text-xs font-medium rounded {splitBadgeClass(snapshot.split)}">
                    {snapshot.split}
                  </span>
                </td>
                <td class="px-4 py-2 text-right text-sm">{snapshot.tp_count}</td>
                <td class="px-4 py-2 text-right text-sm">{snapshot.fp_count}</td>
                <td class="px-4 py-2 text-right text-sm text-gray-500 dark:text-gray-400"
                  >{formatDate(snapshot.created_at)}</td
                >
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
{/if}
