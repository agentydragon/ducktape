<script lang="ts">
  import { resolve } from "$lib/router";

  interface Props {
    href?: string;
    label?: string;
    class?: string;
  }

  let { href: hrefProp, label = "← Back", class: className }: Props = $props();
</script>

<a href={resolve(hrefProp ?? "/")} class={className || "text-sm text-gray-500 hover:text-gray-700 hover:underline"}>
  {label}
</a>
