<script lang="ts">
  import { resolve } from "$lib/router";

  // Link component for agent run IDs (UUIDs)
  // Uses native <a> with SvelteKit client-side navigation

  interface Props {
    id: string;
  }

  let { id }: Props = $props();
</script>

<a href={resolve(`/runs/${id}`)} class="font-mono text-blue-600 underline hover:text-blue-800 truncate" title={id}>
  {id}
</a>
