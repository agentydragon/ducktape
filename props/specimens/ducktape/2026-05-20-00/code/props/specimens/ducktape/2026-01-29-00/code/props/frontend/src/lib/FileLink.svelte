<script lang="ts">
  import { resolve } from "$lib/router";

  // Link component for files in snapshots
  // Links to snapshot detail page with file query parameter
  // Uses native <a> with SvelteKit client-side navigation

  interface Props {
    snapshotSlug: string;
    filePath: string;
    displayText?: string; // Override default filePath display
  }

  let { snapshotSlug, filePath, displayText }: Props = $props();

  const text = $derived(displayText ?? filePath);
</script>

<a
  href={resolve(`/snapshots/${snapshotSlug}?file=${encodeURIComponent(filePath)}`)}
  class="font-mono text-xs text-blue-600 underline hover:text-blue-800"
  title="View {filePath} in {snapshotSlug}"
>
  {text}
</a>
