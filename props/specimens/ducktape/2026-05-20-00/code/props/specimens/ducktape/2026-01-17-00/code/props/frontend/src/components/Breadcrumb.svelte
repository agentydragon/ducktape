<script lang="ts">
  import { ChevronRight } from "lucide-svelte";
  import { resolve } from "$lib/router";

  interface BreadcrumbItem {
    label: string;
    href?: string;
  }

  interface Props {
    items: BreadcrumbItem[];
  }

  let { items }: Props = $props();
</script>

<nav class="flex items-center gap-1 text-sm text-gray-600">
  {#each items as item, i (i)}
    {#if i > 0}
      <ChevronRight size={16} class="text-gray-400" />
    {/if}
    {#if item.href}
      <a href={resolve(item.href)} class="hover:text-gray-900 hover:underline">
        {item.label}
      </a>
    {:else}
      <span class="text-gray-900 font-medium">{item.label}</span>
    {/if}
  {/each}
</nav>
