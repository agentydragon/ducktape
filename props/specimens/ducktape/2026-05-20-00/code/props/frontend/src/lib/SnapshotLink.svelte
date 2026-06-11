<script lang="ts">
  import { resolve } from "$lib/router";
  import { formatSnapshotSlug } from "./formatters";

  // Link component for snapshot slugs
  // Uses native <a> with SvelteKit client-side navigation

  interface Props {
    slug: string;
    showFull?: boolean; // Show full slug instead of first component
  }

  let { slug, showFull = false }: Props = $props();

  const displayText = $derived(showFull ? slug : formatSnapshotSlug(slug));
</script>

<a
  href={resolve(`/snapshots/${slug}`)}
  class="text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
  title={slug}
>
  {displayText}
</a>
