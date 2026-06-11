<script lang="ts">
  import { resolve } from "$lib/router";

  // Link component for TP/FP occurrence IDs
  // Links to snapshot detail page with issue/occurrence and optional file parameter
  // Uses native <a> with SvelteKit client-side navigation

  interface Props {
    snapshotSlug: string;
    issueId: string;
    occurrenceId: string;
    filePath?: string;
    displayText?: string; // Override default "{issueId}/{occurrenceId}" display
  }

  let { snapshotSlug, issueId, occurrenceId, filePath, displayText }: Props = $props();

  const urlPath = $derived.by(() => {
    const url = `/snapshots/${snapshotSlug}/${issueId}/${occurrenceId}`;
    if (filePath) {
      return `${url}?file=${encodeURIComponent(filePath)}`;
    }
    return url;
  });

  const text = $derived(displayText ?? `${issueId}/${occurrenceId}`);
</script>

<a
  href={resolve(urlPath)}
  class="font-mono text-blue-600 underline hover:text-blue-800"
  title="View occurrence {issueId}/{occurrenceId} in {snapshotSlug}"
>
  {text}
</a>
