<script lang="ts">
  import type { GradingEdgeInfo } from "../lib/api/client";
  import CritiqueIssueLink from "../lib/CritiqueIssueLink.svelte";
  import OccurrenceLink from "../lib/OccurrenceLink.svelte";

  interface MissedOccurrence {
    tp_id: string;
    occurrence_id: string;
    tp_rationale: string;
    occ_note?: string;
  }

  interface Props {
    edges: GradingEdgeInfo[];
    missedOccurrences?: MissedOccurrence[];
    totalCredit?: number;
    recallDenominator?: number;
    defaultOpen?: boolean;
    runId?: string; // For linking critique issue IDs
    snapshotSlug?: string; // For linking TP/FP occurrence IDs
  }

  let {
    edges,
    missedOccurrences = [],
    totalCredit,
    recallDenominator,
    defaultOpen = false,
    runId,
    snapshotSlug,
  }: Props = $props();

  const creditSummary = $derived(
    totalCredit != null && recallDenominator != null ? `${totalCredit.toFixed(1)}/${recallDenominator} recall` : null
  );

  // Filter and sort edges - in complete bipartite graph, most edges have zero credit
  // Show ALL non-zero credit edges (TP and FP) prominently, collapse zeros
  const getCredit = (edge: GradingEdgeInfo) => edge.target.credit;

  const nonZeroEdges = $derived(edges.filter((e) => e.target.credit > 0).sort((a, b) => getCredit(b) - getCredit(a)));

  const zeroEdges = $derived(edges.filter((e) => e.target.credit === 0));

  let showZeroEdges = $state(false);
</script>

{#if edges.length > 0 || missedOccurrences.length > 0}
  <details open={defaultOpen}>
    <summary class="cursor-pointer text-gray-500 hover:text-gray-700 text-xs">
      Grading ({nonZeroEdges.length} matches, {zeroEdges.length} non-matches)
      {#if creditSummary}
        <span class="text-gray-400">— {creditSummary}</span>
      {/if}
    </summary>
    <div class="mt-2 space-y-2">
      <!-- Non-zero credit edges (both TP and FP) -->
      {#if nonZeroEdges.length > 0}
        <div class="text-xs font-medium mb-1">Matches ({nonZeroEdges.length}):</div>
        {#each nonZeroEdges as edge (`${edge.critique_issue_id}-${edge.target.kind === "tp" ? edge.target.tp_id : edge.target.fp_id}-${edge.target.occurrence_id}`)}
          {@const target = edge.target}
          {@const credit = getCredit(edge)}
          {@const isTP = target.kind === "tp"}
          {@const bgColor = isTP ? "bg-green-50" : "bg-red-50"}
          {@const borderColor = isTP ? "border-green-200" : "border-red-200"}
          {@const textColor = isTP ? "text-green-600" : "text-red-600"}
          <div class="p-2 rounded border text-xs {bgColor} {borderColor}">
            <div class="flex items-center gap-2 mb-1">
              {#if runId}
                <CritiqueIssueLink {runId} issueId={edge.critique_issue_id} />
              {:else}
                <span class="font-mono font-medium">{edge.critique_issue_id}</span>
              {/if}
              <span class="text-gray-400">→</span>
              {#if target.kind === "tp"}
                {#if snapshotSlug}
                  <span class={textColor}>
                    <OccurrenceLink {snapshotSlug} issueId={target.tp_id} occurrenceId={target.occurrence_id} />
                  </span>
                {:else}
                  <span class={textColor}>{target.tp_id}/{target.occurrence_id}</span>
                {/if}
                <span class="{textColor} font-medium">(+{credit.toFixed(2)})</span>
              {:else if target.kind === "fp"}
                {#if snapshotSlug}
                  <span class={textColor}>
                    <OccurrenceLink {snapshotSlug} issueId={target.fp_id} occurrenceId={target.occurrence_id} />
                  </span>
                {:else}
                  <span class={textColor}>{target.fp_id}/{target.occurrence_id}</span>
                {/if}
                <span class="{textColor} font-medium">(+{credit.toFixed(2)} FP)</span>
              {/if}
            </div>
            <div class="text-gray-600">{edge.rationale}</div>
          </div>
        {/each}
      {/if}

      <!-- Zero-credit edges (collapsed by default) -->
      {#if zeroEdges.length > 0}
        <div class="mt-3 pt-2 border-t border-gray-200">
          <button
            type="button"
            onclick={() => (showZeroEdges = !showZeroEdges)}
            class="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
          >
            <span>{showZeroEdges ? "▼" : "▶"}</span>
            <span>Non-matches ({zeroEdges.length})</span>
          </button>
          {#if showZeroEdges}
            <div class="mt-2 space-y-1">
              {#each zeroEdges as edge (`${edge.critique_issue_id}-${edge.target.kind === "tp" ? edge.target.tp_id : edge.target.fp_id}-${edge.target.occurrence_id}`)}
                {@const target = edge.target}
                <div class="p-2 rounded border text-xs bg-gray-50 border-gray-200">
                  <div class="flex items-center gap-2 mb-1">
                    {#if runId}
                      <span class="text-gray-500"><CritiqueIssueLink {runId} issueId={edge.critique_issue_id} /></span>
                    {:else}
                      <span class="font-mono text-gray-500">{edge.critique_issue_id}</span>
                    {/if}
                    <span class="text-gray-400">→</span>
                    {#if target.kind === "tp"}
                      {#if snapshotSlug}
                        <span class="text-gray-500">
                          <OccurrenceLink {snapshotSlug} issueId={target.tp_id} occurrenceId={target.occurrence_id} />
                        </span>
                      {:else}
                        <span class="text-gray-500">{target.tp_id}/{target.occurrence_id}</span>
                      {/if}
                    {:else if target.kind === "fp"}
                      {#if snapshotSlug}
                        <span class="text-gray-500">
                          <OccurrenceLink {snapshotSlug} issueId={target.fp_id} occurrenceId={target.occurrence_id} />
                          <span> FP</span>
                        </span>
                      {:else}
                        <span class="text-gray-500">{target.fp_id}/{target.occurrence_id} FP</span>
                      {/if}
                    {/if}
                    <span class="text-gray-400">(0.00)</span>
                  </div>
                  <div class="text-gray-500 text-[11px]">{edge.rationale}</div>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/if}

      <!-- Missed occurrences -->
      {#if missedOccurrences.length > 0}
        <div class="mt-3 pt-2 border-t border-gray-200">
          <div class="text-xs font-medium text-red-600 mb-1">Missed ({missedOccurrences.length}):</div>
          {#each missedOccurrences as missed (`${missed.tp_id}-${missed.occurrence_id}`)}
            <div class="p-2 rounded border text-xs bg-red-50 border-red-200">
              <div class="flex items-center gap-2">
                {#if snapshotSlug}
                  <span class="font-mono font-medium text-red-700">
                    <OccurrenceLink {snapshotSlug} issueId={missed.tp_id} occurrenceId={missed.occurrence_id} />
                  </span>
                {:else}
                  <span class="font-mono font-medium text-red-700">{missed.tp_id}/{missed.occurrence_id}</span>
                {/if}
              </div>
              <div class="text-gray-600 mt-1">{missed.tp_rationale}</div>
              {#if missed.occ_note}
                <div class="text-gray-500 italic mt-1">{missed.occ_note}</div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </details>
{/if}
