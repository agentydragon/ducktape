<script lang="ts">
  import { resolve } from "$lib/router";

  // Link component for agent definition IDs
  // Uses native <a> with SvelteKit client-side navigation

  interface Props {
    id: string;
  }

  let { id }: Props = $props();
</script>

<a href={resolve(`/definitions/${id}`)} class="text-blue-600 underline hover:text-blue-800">
  {id}
</a>
