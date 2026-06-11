<script lang="ts">
  import type { Snippet } from "svelte";
  import { formatJson } from "../lib/llmRequestUtils";

  interface Props {
    item: Record<string, unknown>;
    /** Extra flex alignment class for the content row, e.g. "items-start" for <pre> content. */
    alignItems?: string;
    children: Snippet<[]>;
  }
  let { item, alignItems, children }: Props = $props();
</script>

<div class="space-y-1">
  <div class="flex gap-2 {alignItems ?? ''}">
    {@render children()}
  </div>
  <details>
    <summary class="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 cursor-pointer list-none"
      >Raw</summary
    >
    <pre class="mt-1 bg-gray-900 text-gray-100 p-2 rounded text-xs overflow-auto max-h-40">{formatJson(item)}</pre>
  </details>
</div>
