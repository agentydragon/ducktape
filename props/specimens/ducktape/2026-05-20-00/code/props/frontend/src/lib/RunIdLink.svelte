<script lang="ts">
  import { resolve } from "$lib/router";
  import { formatUuid } from "$lib/formatters";

  // Link component for agent run IDs (UUIDs)
  // Uses native <a> with SvelteKit client-side navigation

  interface Props {
    id: string;
  }

  let { id }: Props = $props();
</script>

<a
  href={resolve(`/runs/${id}`)}
  class="font-mono text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
  title={id}
>
  {formatUuid(id)}
</a>
