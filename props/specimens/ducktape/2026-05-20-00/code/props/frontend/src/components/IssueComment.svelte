<script lang="ts">
  import { CheckCircle, XCircle, Link } from "lucide-svelte";
  import type { GradingEdgeInfo, LocationAnchor } from "../lib/api/client";
  import { issueColors } from "../lib/colors";
  import { formatLocationAnchor } from "../lib/formatters";
  import OccurrenceLink from "../lib/OccurrenceLink.svelte";
  import CopyButton from "./CopyButton.svelte";

  interface Props {
    kind: "tp" | "fp" | "critique";
    issueId: string;
    rationale: string;
    note?: string;
    allLocations?: LocationAnchor[];
    expanded?: boolean;
    onToggle?: () => void;
    gradingEdges?: GradingEdgeInfo[]; // For critique issues - show what they matched
    credit?: number; // For grading edge targets
    copyUrl?: string; // Optional URL to copy for this occurrence
    snapshotSlug?: string; // For linking grading edge targets (TP/FP occurrences)
  }

  let {
    kind,
    issueId,
    rationale,
    note,
    allLocations = [],
    expanded = false,
    onToggle,
    gradingEdges = [],
    credit,
    copyUrl,
    snapshotSlug,
  }: Props = $props();

  // Helper to create styling from colors and label
  const createStyling = (
    colors: { bg: string; border: string; borderLeft: string; headerBg: string; text: string; textDark: string },
    label: string
  ) => ({
    ...colors,
    iconColor: colors.text,
    label,
    labelColor: colors.textDark,
  });

  // Icon mappings
  const ICONS = {
    tp: CheckCircle,
    fp: XCircle,
    critique: Link,
  } as const;

  // Static color classes for grading edge targets — Tailwind scanner needs literal strings
  const TARGET_STYLES = {
    tp: {
      bg: "bg-green-50 dark:bg-green-950",
      border: "border-green-200 dark:border-green-800",
      iconColor: "text-green-600 dark:text-green-400",
      textColor: "text-green-700 dark:text-green-300",
      creditColor: "text-green-600 dark:text-green-400",
    },
    fp: {
      bg: "bg-red-50 dark:bg-red-950",
      border: "border-red-200 dark:border-red-800",
      iconColor: "text-red-600 dark:text-red-400",
      textColor: "text-red-700 dark:text-red-300",
      creditColor: "text-red-600 dark:text-red-400",
    },
  } as const;

  // Get label for grading edge target
  const getTargetLabel = (target: { kind: "tp" | "fp"; tp_id?: string; fp_id?: string; occurrence_id?: string }) => {
    if (target.kind === "tp") return `${target.tp_id}/${target.occurrence_id}`;
    return `${target.fp_id}/${target.occurrence_id}`;
  };

  // Compute critique classification once
  const critiqueType = $derived.by(() => {
    if (kind !== "critique") return null;
    const hasTPMatch = gradingEdges.some((e) => e.target.kind === "tp" && e.target.credit > 0);
    const hasFPMatch = gradingEdges.some((e) => e.target.kind === "fp" && e.target.credit > 0);
    if (hasTPMatch) return "tp";
    if (hasFPMatch) return "fp";
    return "default";
  });

  const Icon = $derived.by(() => {
    if (kind === "tp") return ICONS.tp;
    if (kind === "fp") return ICONS.fp;
    return ICONS.critique;
  });

  const styling = $derived.by(() => {
    if (kind === "tp") return createStyling(issueColors.tp, "TP");
    if (kind === "fp") return createStyling(issueColors.fp, "FP");

    // Critique styling based on grading
    if (critiqueType === "tp") return createStyling(issueColors.critique, "Critique (TP)");
    if (critiqueType === "fp") return createStyling(issueColors.critiqueFp, "Critique (FP)");
    return createStyling(issueColors.critique, "Critique");
  });
</script>

<div class="border-l-4 {styling.border} {styling.bg} rounded-r shadow-sm my-2">
  <!-- Header -->
  <button
    class="w-full px-3 py-2 {styling.headerBg} flex items-center gap-2 hover:opacity-80 transition-opacity"
    onclick={onToggle}
    type="button"
  >
    <Icon size={16} class={styling.iconColor} />
    <span class="font-mono text-sm font-medium">{issueId}</span>
    <span class="text-xs {styling.labelColor} font-medium">{styling.label}</span>
    {#if credit !== undefined}
      <span class="text-xs text-gray-500 dark:text-gray-400">(+{credit.toFixed(2)})</span>
    {/if}
    <span class="ml-auto text-gray-400 dark:text-gray-500 text-xs">{expanded ? "▼" : "▶"}</span>
  </button>

  <!-- Content (expanded) -->
  {#if expanded}
    <div class="px-3 py-2 space-y-2 text-sm">
      {#if copyUrl}
        <div class="flex justify-end">
          <CopyButton text={copyUrl} label="Copy Link" />
        </div>
      {/if}
      <div>
        <div class="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Rationale:</div>
        <div class="text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{rationale}</div>
      </div>

      {#if note}
        <div>
          <div class="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Note:</div>
          <div class="text-gray-700 dark:text-gray-300 italic">{note}</div>
        </div>
      {/if}

      {#if allLocations.length > 1}
        <div>
          <div class="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">All locations:</div>
          {#each allLocations as loc, i (`${loc.file}-${loc.start_line}-${i}`)}
            <div class="font-mono text-xs text-gray-700 dark:text-gray-300">
              {formatLocationAnchor(loc)}
              {#if loc.note}
                <span class="italic text-gray-500 dark:text-gray-400 ml-1">({loc.note})</span>
              {/if}
            </div>
          {/each}
        </div>
      {/if}

      {#if kind === "critique" && gradingEdges.length > 0}
        <div>
          <div class="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Grading:</div>
          <div class="space-y-1">
            {#each gradingEdges as edge (`${edge.critique_issue_id}-${edge.target.kind === "tp" ? edge.target.tp_id : edge.target.fp_id}-${edge.target.occurrence_id}`)}
              {@const target = edge.target}
              {#if target.credit > 0}
                {@const TargetIcon = ICONS[target.kind]}
                {@const ts = TARGET_STYLES[target.kind]}
                <div class="text-xs p-1.5 rounded border {ts.bg} {ts.border}">
                  <div class="flex items-center gap-2">
                    <TargetIcon size={12} class={ts.iconColor} />
                    <span class="font-mono">
                      {#if snapshotSlug && target.kind === "tp" && target.tp_id && target.occurrence_id}
                        <OccurrenceLink {snapshotSlug} issueId={target.tp_id} occurrenceId={target.occurrence_id} />
                      {:else if snapshotSlug && target.kind === "fp" && target.fp_id && target.occurrence_id}
                        <OccurrenceLink {snapshotSlug} issueId={target.fp_id} occurrenceId={target.occurrence_id} />
                      {:else}
                        <span class={ts.textColor}>{getTargetLabel(target)}</span>
                      {/if}
                    </span>
                    <span class="{ts.creditColor} font-medium">(+{target.credit.toFixed(2)})</span>
                  </div>
                  {#if edge.rationale}
                    <div class="text-gray-600 dark:text-gray-400 mt-1">{edge.rationale}</div>
                  {/if}
                </div>
              {/if}
            {/each}
          </div>
        </div>
      {/if}
    </div>
  {/if}
</div>
