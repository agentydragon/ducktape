<script lang="ts">
  import CopyButton from "./CopyButton.svelte";
  import { formatJson } from "../lib/llmRequestUtils";

  interface Props {
    title: string;
    value: unknown;
  }

  const { title, value }: Props = $props();
  const json = $derived(formatJson(value));
</script>

<div class="p-4 space-y-3">
  <div class="flex items-center justify-between">
    <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">{title}</h4>
    <CopyButton text={json} label="Copy JSON" />
  </div>
  <pre class="bg-gray-50 dark:bg-gray-900 p-3 rounded text-xs overflow-auto">{json}</pre>
</div>
