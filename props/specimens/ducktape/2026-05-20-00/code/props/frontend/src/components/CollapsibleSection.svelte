<script lang="ts">
  import { ChevronRight, ChevronDown } from "lucide-svelte";
  import { formatJson } from "../lib/llmRequestUtils";

  interface Props {
    label: string;
    jsonData: Record<string, unknown>;
  }
  let { label, jsonData }: Props = $props();
</script>

<details class="group">
  <summary
    class="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 cursor-pointer list-none"
  >
    <ChevronDown size={12} class="hidden group-open:block" />
    <ChevronRight size={12} class="block group-open:hidden" />
    {label}
  </summary>
  <pre class="mt-1 bg-gray-900 text-gray-100 p-2 rounded text-xs overflow-auto max-h-48">{formatJson(jsonData)}</pre>
</details>
