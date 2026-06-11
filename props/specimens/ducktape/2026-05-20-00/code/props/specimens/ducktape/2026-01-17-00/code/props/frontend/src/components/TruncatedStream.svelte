<script lang="ts">
  import type { TruncatedStream } from "../lib/api/client";

  interface Props {
    stream: string | TruncatedStream;
    kind: "stdout" | "stderr";
  }

  let { stream, kind }: Props = $props();

  const text = $derived(typeof stream === "string" ? stream : stream.truncated_text);

  const isTruncated = $derived(typeof stream !== "string");

  const totalBytes = $derived(typeof stream === "string" ? undefined : stream.total_bytes);

  const colorClass = $derived(kind === "stdout" ? "text-gray-200" : "text-red-400");
</script>

{#if text}
  <pre class="whitespace-pre-wrap break-words {colorClass}">{text}</pre>
  {#if isTruncated}
    <div class="text-yellow-500 mt-1">
      ... {kind} truncated ({totalBytes?.toLocaleString()} bytes total)
    </div>
  {/if}
{/if}
