<script lang="ts">
  interface Props {
    meanCredit: number;
    nRuns: number;
  }

  let { meanCredit, nRuns }: Props = $props();

  const colorClass = $derived(
    meanCredit >= 0.7
      ? "bg-green-100 text-green-700 border-green-300 dark:bg-green-900 dark:text-green-300 dark:border-green-700"
      : meanCredit >= 0.4
        ? "bg-yellow-100 text-yellow-700 border-yellow-300 dark:bg-yellow-900 dark:text-yellow-300 dark:border-yellow-700"
        : "bg-red-100 text-red-700 border-red-300 dark:bg-red-900 dark:text-red-300 dark:border-red-700"
  );
</script>

<span
  class="inline-flex items-center gap-1 px-1.5 py-0.5 text-xs font-medium rounded border {colorClass}"
  title="Mean credit: {(meanCredit * 100).toFixed(1)}% ({nRuns} runs)"
>
  {(meanCredit * 100).toFixed(0)}%
  <span class="text-gray-400 dark:text-gray-500">({nRuns})</span>
</span>
