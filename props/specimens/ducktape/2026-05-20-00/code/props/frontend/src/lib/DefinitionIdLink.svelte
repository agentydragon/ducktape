<script lang="ts">
  import { resolve } from "$lib/router";
  import { formatDigest } from "$lib/formatters";

  interface Props {
    id: string;
    display_name?: string | null;
  }

  let { id, display_name }: Props = $props();
</script>

<a
  href={resolve(`/definitions/${id}`)}
  class="text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
  title={id}
>
  {display_name ?? formatDigest(id)}
</a>
