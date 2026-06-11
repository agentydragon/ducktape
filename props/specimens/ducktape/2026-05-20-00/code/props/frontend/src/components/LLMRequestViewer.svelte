<script lang="ts">
  import { ChevronRight, ChevronDown } from "lucide-svelte";
  import type { LLMRequestInfo } from "../lib/api/client";
  import LLMRequestSection from "./LLMRequestSection.svelte";
  import LLMResponseSection from "./LLMResponseSection.svelte";

  interface Props {
    requests: LLMRequestInfo[];
    initialExpanded?: number[];
  }
  let { requests, initialExpanded = [] }: Props = $props();
</script>

{#if requests.length === 0}
  <p class="text-gray-500 dark:text-gray-400 italic">No LLM requests recorded</p>
{:else}
  <div class="space-y-2">
    {#each requests as req (req.id)}
      <details open={initialExpanded.includes(req.id)} class="border dark:border-gray-700 rounded group">
        <summary
          class="px-4 py-2 flex items-center justify-between cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 list-none"
        >
          <div class="flex items-center gap-4 text-sm">
            <span class="font-mono text-gray-500 dark:text-gray-400">#{req.id}</span>
            <span class="font-medium">{req.model}</span>
            {#if req.latency_ms}
              <span class="text-gray-500 dark:text-gray-400">{req.latency_ms}ms</span>
            {/if}
            {#if req.error}
              <span class="text-red-600 dark:text-red-400">Error</span>
            {/if}
          </div>
          <span class="text-gray-400 dark:text-gray-500">
            <ChevronDown size={16} class="hidden group-open:block" />
            <ChevronRight size={16} class="block group-open:hidden" />
          </span>
        </summary>

        <div class="border-t dark:border-gray-700 divide-y dark:divide-gray-700">
          <LLMRequestSection requestBody={req.request_body as Record<string, unknown>} />
          {#if req.response_body}
            <LLMResponseSection responseBody={req.response_body as Record<string, unknown>} />
          {/if}
          {#if req.response_error_body}
            <div class="p-4">
              <h4 class="text-xs font-semibold uppercase tracking-wide text-red-600 dark:text-red-400 mb-2">
                Error Response
              </h4>
              <pre
                class="bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300 p-3 rounded text-xs overflow-auto">{JSON.stringify(
                  req.response_error_body,
                  null,
                  2
                )}</pre>
            </div>
          {/if}
          {#if req.error}
            <div class="p-4">
              <h4 class="text-xs font-semibold uppercase tracking-wide text-red-600 dark:text-red-400 mb-2">Error</h4>
              <pre
                class="bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300 p-3 rounded text-xs">{req.error}</pre>
            </div>
          {/if}
        </div>
      </details>
    {/each}
  </div>
{/if}
