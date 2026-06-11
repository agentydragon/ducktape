<script lang="ts">
  import { resolve } from "$lib/router";

  // Link component for TP/FP issue IDs
  // Links to snapshot detail page with anchor to issue section
  // Uses native <a> with SvelteKit client-side navigation

  interface Props {
    snapshotSlug: string;
    issueId: string;
    kind: "tp" | "fp";
    displayText?: string; // Override default issueId display
  }

  let { snapshotSlug, issueId, kind, displayText }: Props = $props();

  const text = $derived(displayText ?? issueId);
</script>

<a
  href={resolve(`/snapshots/${snapshotSlug}#${kind}-${issueId}`)}
  class="font-mono text-blue-600 underline hover:text-blue-800"
  title="View {kind.toUpperCase()} {issueId} in {snapshotSlug}"
>
  {text}
</a>
