<script lang="ts">
  import { SvelteSet } from "svelte/reactivity";
  import type { LLMRequestInfo } from "../lib/api/client";

  interface Props {
    requests: LLMRequestInfo[];
  }
  let { requests }: Props = $props();

  let expandedRequests = $state(new SvelteSet<number>());

  function toggleRequest(id: number) {
    if (expandedRequests.has(id)) {
      expandedRequests.delete(id);
    } else {
      expandedRequests.add(id);
    }
  }
</script>

{#if requests.length === 0}
  <p class="text-gray-500 italic">No LLM requests recorded</p>
{:else}
  <div class="space-y-2">
    {#each requests as req (req.id)}
      <div class="border rounded">
        <button
          class="w-full px-4 py-2 flex items-center justify-between text-left hover:bg-gray-50"
          onclick={() => toggleRequest(req.id)}
        >
          <div class="flex items-center gap-4 text-sm">
            <span class="font-mono text-gray-500">#{req.id}</span>
            <span class="font-medium">{req.model}</span>
            {#if req.latency_ms}
              <span class="text-gray-500">{req.latency_ms}ms</span>
            {/if}
            {#if req.error}
              <span class="text-red-600">Error</span>
            {/if}
          </div>
          <span class="text-gray-400">{expandedRequests.has(req.id) ? "▼" : "▶"}</span>
        </button>
        {#if expandedRequests.has(req.id)}
          <div class="border-t p-4 space-y-4">
            <div>
              <h4 class="text-sm font-medium text-gray-600 mb-2">Request</h4>
              <pre class="bg-gray-900 text-gray-100 p-3 rounded text-xs overflow-auto max-h-64">{JSON.stringify(
                  req.request_body,
                  null,
                  2
                )}</pre>
            </div>
            {#if req.response_body}
              <div>
                <h4 class="text-sm font-medium text-gray-600 mb-2">Response</h4>
                <pre class="bg-gray-900 text-gray-100 p-3 rounded text-xs overflow-auto max-h-64">{JSON.stringify(
                    req.response_body,
                    null,
                    2
                  )}</pre>
              </div>
            {/if}
            {#if req.error}
              <div>
                <h4 class="text-sm font-medium text-red-600 mb-2">Error</h4>
                <pre class="bg-red-50 text-red-700 p-3 rounded text-xs">{req.error}</pre>
              </div>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>
{/if}
