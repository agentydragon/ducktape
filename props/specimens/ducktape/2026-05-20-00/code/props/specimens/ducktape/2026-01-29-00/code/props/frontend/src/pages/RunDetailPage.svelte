<script lang="ts">
  import RunDetail from "$components/RunDetail.svelte";

  interface Props {
    runId: string;
  }
  let { runId }: Props = $props();
</script>

{#if runId}
  <RunDetail {runId} />
{:else}
  <p class="text-gray-500">Invalid run ID</p>
{/if}
