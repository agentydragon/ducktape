<script lang="ts">
  import { Copy, Check } from "lucide-svelte";
  import { toast } from "svelte-sonner";

  interface Props {
    text: string;
    label?: string;
    successMessage?: string;
  }

  let { text, label = "Copy", successMessage = "Copied to clipboard" }: Props = $props();

  let copied = $state(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
      toast.success(successMessage);
      setTimeout(() => (copied = false), 2000);
    } catch (err) {
      toast.error("Failed to copy to clipboard");
      console.error("Copy failed:", err);
    }
  }
</script>

<button
  onclick={copy}
  type="button"
  class="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-gray-700 bg-white border border-gray-300 rounded hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500"
  title={label}
>
  {#if copied}
    <Check size={14} class="text-green-600" />
    <span class="text-green-600">Copied!</span>
  {:else}
    <Copy size={14} />
    <span>{label}</span>
  {/if}
</button>
