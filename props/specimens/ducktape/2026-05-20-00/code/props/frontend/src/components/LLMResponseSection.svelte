<script lang="ts">
  import CopyButton from "./CopyButton.svelte";
  import ExpandableItem from "./ExpandableItem.svelte";
  import CollapsibleSection from "./CollapsibleSection.svelte";
  import { formatJson, tryParseJson, getContentText } from "../lib/llmRequestUtils";

  interface Props {
    responseBody: Record<string, unknown>;
  }
  let { responseBody }: Props = $props();

  const outputItems = Array.isArray(responseBody.output) ? (responseBody.output as Record<string, unknown>[]) : null;
  const usage = responseBody.usage as Record<string, unknown> | null | undefined;
  const detailKeys = Object.keys(responseBody).filter((k) => k !== "output" && k !== "usage");
</script>

<div class="p-4 space-y-2">
  <div class="flex items-center justify-between mb-3">
    <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Response</h4>
    <CopyButton text={formatJson(responseBody)} label="Copy JSON" />
  </div>

  <!-- Output items -->
  {#if outputItems}
    {#each outputItems as item}
      {@const itype = typeof item.type === "string" ? item.type : null}
      {#if itype === "message"}
        <ExpandableItem {item}>
          <p class="flex-1 text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">
            {getContentText(item.content)}
          </p>
        </ExpandableItem>
      {:else if itype === "function_call"}
        <ExpandableItem {item} alignItems="items-start">
          <span
            class="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300"
            >⚙ {item.name}</span
          >
          <pre
            class="flex-1 min-w-0 text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap overflow-auto max-h-32">{formatJson(
              tryParseJson(item.arguments)
            )}</pre>
        </ExpandableItem>
      {:else if itype === "reasoning"}
        <ExpandableItem {item}>
          <span
            class="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300"
            >💭 reasoning</span
          >
          <p class="flex-1 text-sm text-gray-600 dark:text-gray-400 italic">
            {getContentText(item.summary)}
          </p>
        </ExpandableItem>
      {/if}
    {/each}
  {/if}

  <!-- Token usage summary -->
  {#if usage}
    {@const inputDetails = usage.input_tokens_details as Record<string, unknown> | null | undefined}
    {@const outputDetails = usage.output_tokens_details as Record<string, unknown> | null | undefined}
    {@const cached = typeof inputDetails?.cached_tokens === "number" ? inputDetails.cached_tokens : 0}
    {@const reasoning = typeof outputDetails?.reasoning_tokens === "number" ? outputDetails.reasoning_tokens : 0}
    <p class="text-xs text-gray-500 dark:text-gray-400">
      ↑ {usage.input_tokens} in{cached > 0 ? ` (${cached} cached)` : ""}
      · ↓ {usage.output_tokens} out{reasoning > 0 ? ` (${reasoning} reasoning)` : ""}
    </p>
  {/if}

  <!-- Response details (everything except output/usage) -->
  {#if detailKeys.length > 0}
    <CollapsibleSection
      label={`Response details (${detailKeys.join(", ")})`}
      jsonData={Object.fromEntries(detailKeys.map((k) => [k, responseBody[k]]))}
    />
  {/if}
</div>
