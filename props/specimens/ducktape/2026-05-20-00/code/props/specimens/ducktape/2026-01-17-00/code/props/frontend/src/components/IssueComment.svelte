<script lang="ts">
  import { CheckCircle, XCircle, Link } from "lucide-svelte";
  import type { GradingEdgeInfo, FileLocationInfo } from "../lib/api/client";
  import { issueColors } from "../lib/colors";
  import { formatFileLocation } from "../lib/formatters";
  import OccurrenceLink from "../lib/OccurrenceLink.svelte";
  import CopyButton from "./CopyButton.svelte";

  interface Props {
    kind: "tp" | "fp" | "critique";
    issueId: string;
    rationale: string;
    note?: string;
    allFiles?: FileLocationInfo[];
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
    allFiles = [],
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

  // Target kind to color mapping
  const TARGET_COLORS = {
    tp: "green",
    fp: "red",
  } as const;

  // Helper to create color classes for a given base color
  const colorClasses = (color: string, creditColor?: string) => ({
    bg: `bg-${color}-50`,
    border: `border-${color}-200`,
    iconColor: `text-${color}-600`,
    textColor: `text-${color}-700`,
    creditColor: creditColor ?? `text-${color}-600`,
  });

  // Helper to create grading edge styling from base color and label
  const createTargetStyling = (baseColor: string, label: string, creditColor?: string) => ({
    ...colorClasses(baseColor, creditColor),
    label,
  });

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
      <span class="text-xs text-gray-500">(+{credit.toFixed(2)})</span>
    {/if}
    <span class="ml-auto text-gray-400 text-xs">{expanded ? "▼" : "▶"}</span>
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
        <div class="text-xs font-medium text-gray-600 mb-1">Rationale:</div>
        <div class="text-gray-800 whitespace-pre-wrap">{rationale}</div>
      </div>

      {#if note}
        <div>
          <div class="text-xs font-medium text-gray-600 mb-1">Note:</div>
          <div class="text-gray-700 italic">{note}</div>
        </div>
      {/if}

      {#if allFiles.length > 1}
        <div>
          <div class="text-xs font-medium text-gray-600 mb-1">All affected files:</div>
          {#each allFiles as file (file.path)}
            <div class="font-mono text-xs text-gray-700">
              {formatFileLocation(file)}
            </div>
          {/each}
        </div>
      {/if}

      {#if kind === "critique" && gradingEdges.length > 0}
        <div>
          <div class="text-xs font-medium text-gray-600 mb-1">Grading:</div>
          <div class="space-y-1">
            {#each gradingEdges as edge (`${edge.critique_issue_id}-${edge.target.kind === "tp" ? edge.target.tp_id : edge.target.fp_id}-${edge.target.occurrence_id}`)}
              {@const target = edge.target}
              {#if target.credit > 0}
                {@const TargetIcon = ICONS[target.kind]}
                {@const targetStyling = createTargetStyling(TARGET_COLORS[target.kind], getTargetLabel(target))}
                <div class="text-xs p-1.5 rounded border {targetStyling.bg} {targetStyling.border}">
                  <div class="flex items-center gap-2">
                    <TargetIcon size={12} class={targetStyling.iconColor} />
                    <span class="font-mono">
                      {#if snapshotSlug && target.kind === "tp" && target.tp_id && target.occurrence_id}
                        <OccurrenceLink {snapshotSlug} issueId={target.tp_id} occurrenceId={target.occurrence_id} />
                      {:else if snapshotSlug && target.kind === "fp" && target.fp_id && target.occurrence_id}
                        <OccurrenceLink {snapshotSlug} issueId={target.fp_id} occurrenceId={target.occurrence_id} />
                      {:else}
                        <span class={targetStyling.textColor}>{targetStyling.label}</span>
                      {/if}
                    </span>
                    <span class="{targetStyling.creditColor} font-medium">(+{target.credit.toFixed(2)})</span>
                  </div>
                  {#if edge.rationale}
                    <div class="text-gray-600 mt-1">{edge.rationale}</div>
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
