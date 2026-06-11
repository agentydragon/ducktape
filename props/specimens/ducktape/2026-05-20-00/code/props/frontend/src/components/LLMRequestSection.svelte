<script lang="ts">
  import CopyButton from "./CopyButton.svelte";
  import ExpandableItem from "./ExpandableItem.svelte";
  import CollapsibleSection from "./CollapsibleSection.svelte";
  import { formatJson, tryParseJson, getContentText, roleBadgeClass } from "../lib/llmRequestUtils";

  interface Props {
    requestBody: Record<string, unknown>;
  }
  let { requestBody }: Props = $props();

  const inputItems = Array.isArray(requestBody.input) ? (requestBody.input as Record<string, unknown>[]) : null;
  const inputStr = typeof requestBody.input === "string" ? requestBody.input : null;
  const instructions = typeof requestBody.instructions === "string" ? requestBody.instructions : null;
  const paramKeys = Object.keys(requestBody).filter((k) => k !== "input" && k !== "instructions");
</script>

<div class="p-4 space-y-2">
  <div class="flex items-center justify-between mb-3">
    <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Request</h4>
    <CopyButton text={formatJson(requestBody)} label="Copy JSON" />
  </div>

  <!-- System instructions (no Raw toggle — not a conversation item) -->
  {#if instructions}
    <div class="flex gap-2">
      <span class="shrink-0 px-2 py-0.5 text-xs font-medium rounded {roleBadgeClass.system}"> instructions </span>
      <p class="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{instructions}</p>
    </div>
  {/if}

  <!-- Input conversation items -->
  {#if inputItems}
    {#each inputItems as item}
      {@const role = typeof item.role === "string" ? item.role : null}
      {@const itype = typeof item.type === "string" ? item.type : null}
      {#if role}
        <ExpandableItem {item}>
          <span class="shrink-0 px-2 py-0.5 text-xs font-medium rounded {roleBadgeClass[role] ?? roleBadgeClass.system}"
            >{role}</span
          >
          <p class="flex-1 min-w-0 text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">
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
      {:else if itype === "function_call_output"}
        <ExpandableItem {item} alignItems="items-start">
          <span
            class="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300"
            >↩ result</span
          >
          <pre
            class="flex-1 min-w-0 text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap overflow-auto max-h-32">{formatJson(
              tryParseJson(item.output)
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
  {:else if inputStr}
    <p class="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{inputStr}</p>
  {/if}

  <!-- Request params (everything except input/instructions) -->
  {#if paramKeys.length > 0}
    <CollapsibleSection
      label={`Request params (${paramKeys.join(", ")})`}
      jsonData={Object.fromEntries(paramKeys.map((k) => [k, requestBody[k]]))}
    />
  {/if}
</div>
