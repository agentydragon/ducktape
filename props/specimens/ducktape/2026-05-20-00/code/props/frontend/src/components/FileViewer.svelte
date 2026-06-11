<script lang="ts">
  import "highlight.js/styles/github.css";
  import { resolve } from "$lib/router";
  import { SvelteMap, SvelteSet } from "svelte/reactivity";
  import { CheckCircle, XCircle, MessageSquare, ChevronRight, ChevronDown } from "lucide-svelte";
  import type {
    FileContentResponse,
    TpInfo,
    FpInfo,
    GradingEdgeInfo,
    ReportedIssueInfo,
    ReportedIssueOccurrenceInfo,
  } from "../lib/api/client";
  import type { IssueMarker, LocationAnchor } from "../lib/types";
  import IssueComment from "./IssueComment.svelte";
  import { detectLanguage } from "../lib/fileTypes";
  import { highlightLines } from "../lib/highlighting";

  /** Get locations for a specific file from an issue marker. */
  function getLocationsForFile(marker: IssueMarker, filePath: string): LocationAnchor[] {
    return marker.allLocations.filter((loc) => loc.file === filePath);
  }

  interface Props {
    file: FileContentResponse;
    tps?: TpInfo[];
    fps?: FpInfo[];
    critiqueIssues?: ReportedIssueInfo[];
    gradingEdges?: GradingEdgeInfo[];
    snapshotSlug?: string;
    targetOccurrenceId?: string | null;
    defaultCollapsed?: boolean;
  }

  let {
    file,
    tps = [],
    fps = [],
    critiqueIssues = [],
    gradingEdges = [],
    snapshotSlug,
    targetOccurrenceId = null,
    defaultCollapsed = false,
  }: Props = $props();

  let collapsed = $state(defaultCollapsed);

  const lines = $derived.by(() => {
    const raw = file.content.split("\n");
    // Remove trailing empty string from trailing newline (most source files end with \n)
    if (raw.length > 1 && raw[raw.length - 1] === "") return raw.slice(0, -1);
    return raw;
  });
  const language = $derived(detectLanguage(file.path));
  const highlightedLines = $derived(highlightLines(lines, language));

  // Combine all issues (TPs, FPs, and optionally critique issues) that reference this file
  const allIssues = $derived.by<IssueMarker[]>(() => {
    const result: IssueMarker[] = [];

    for (const tp of tps) {
      for (const occ of tp.occurrences) {
        if (occ.locations.some((loc) => loc.file === file.path)) {
          result.push({
            kind: "tp",
            issueId: tp.tp_id,
            occurrenceId: occ.occurrence_id,
            rationale: tp.rationale,
            note: occ.note ?? undefined,
            allLocations: occ.locations,
          });
        }
      }
    }

    for (const fp of fps) {
      for (const occ of fp.occurrences) {
        if (occ.locations.some((loc) => loc.file === file.path)) {
          result.push({
            kind: "fp",
            issueId: fp.fp_id,
            occurrenceId: occ.occurrence_id,
            rationale: fp.rationale,
            note: occ.note ?? undefined,
            allLocations: occ.locations,
          });
        }
      }
    }

    for (const issue of critiqueIssues) {
      const issueAllLocations = issue.occurrences.flatMap((occ: ReportedIssueOccurrenceInfo) => occ.locations);
      if (issueAllLocations.some((loc) => loc.file === file.path)) {
        const edges = gradingEdges.filter((e) => e.critique_issue_id === issue.issue_id);
        const note = issue.occurrences[0]?.note ?? undefined;
        result.push({
          kind: "critique",
          issueId: issue.issue_id,
          rationale: issue.rationale,
          note,
          allLocations: issueAllLocations,
          gradingEdges: edges,
        });
      }
    }

    return result;
  });

  // Map line numbers to issues (0-based line index)
  const lineToIssues = $derived.by<SvelteMap<number, IssueMarker[]>>(() => {
    const map = new SvelteMap<number, IssueMarker[]>();

    for (const issue of allIssues) {
      const locs = getLocationsForFile(issue, file.path);
      if (locs.length === 0 || locs.every((loc) => loc.start_line == null)) {
        // Whole-file: mark every line
        for (let i = 0; i < lines.length; i++) {
          const existing = map.get(i) || [];
          map.set(i, [...existing, issue]);
        }
      } else {
        for (const loc of locs) {
          if (loc.start_line == null) continue;
          const startIdx = loc.start_line - 1;
          const endIdx = (loc.end_line ?? loc.start_line) - 1;
          for (let i = startIdx; i <= endIdx; i++) {
            const existing = map.get(i) || [];
            map.set(i, [...existing, issue]);
          }
        }
      }
    }

    return map;
  });

  // Map line numbers to location notes (show after the last line of each location with a note)
  const lineToLocationNotes = $derived.by<SvelteMap<number, Array<{ issue: IssueMarker; loc: LocationAnchor }>>>(() => {
    const map = new SvelteMap<number, Array<{ issue: IssueMarker; loc: LocationAnchor }>>();

    for (const issue of allIssues) {
      const locs = getLocationsForFile(issue, file.path);
      for (const loc of locs) {
        if (loc.note && loc.start_line != null) {
          const endIdx = (loc.end_line ?? loc.start_line) - 1;
          const existing = map.get(endIdx) || [];
          map.set(endIdx, [...existing, { issue, loc }]);
        }
      }
    }

    return map;
  });

  let expandedIssues = $state(new SvelteSet<string>());

  function toggleIssue(id: string) {
    if (expandedIssues.has(id)) {
      expandedIssues.delete(id);
    } else {
      expandedIssues.add(id);
    }
  }

  function getIssueKey(issue: IssueMarker): string {
    return issue.occurrenceId
      ? `${issue.kind}-${issue.issueId}-${issue.occurrenceId}`
      : `${issue.kind}-${issue.issueId}`;
  }

  function getOccurrenceUrl(issueId: string, occurrenceId: string): string | undefined {
    if (!snapshotSlug) return undefined;
    const routePath = `/snapshots/${snapshotSlug}/${issueId}/${occurrenceId}?file=${encodeURIComponent(file.path)}`;
    return `${window.location.origin}${resolve(routePath)}`;
  }

  const tpCount = $derived(allIssues.filter((i) => i.kind === "tp").length);
  const fpCount = $derived(allIssues.filter((i) => i.kind === "fp").length);
  const critiqueCount = $derived(allIssues.filter((i) => i.kind === "critique").length);
  const hasCritiques = $derived(critiqueIssues.length > 0);
</script>

<div class="border dark:border-gray-700 rounded bg-white dark:bg-gray-900 font-mono text-sm">
  <!-- Header (clickable to toggle collapse) -->
  <button
    class="px-4 py-2 bg-gray-50 dark:bg-gray-800 flex items-center gap-2 w-full text-left cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 {collapsed
      ? ''
      : 'border-b dark:border-gray-700'}"
    onclick={() => (collapsed = !collapsed)}
  >
    {#if collapsed}
      <ChevronRight size={16} class="text-gray-500 dark:text-gray-400 flex-shrink-0" />
    {:else}
      <ChevronDown size={16} class="text-gray-500 dark:text-gray-400 flex-shrink-0" />
    {/if}
    <span class="font-semibold">{file.path}</span>
    <span class="text-gray-500 dark:text-gray-400 text-xs">({file.line_count} lines)</span>
    <span class="text-gray-500 dark:text-gray-400 text-xs ml-auto">
      {#if hasCritiques}
        {critiqueCount} critique,
      {/if}
      {tpCount} TPs,
      {fpCount} FPs
    </span>
  </button>

  <!-- Content -->
  {#if !collapsed}
    <div class="overflow-auto max-h-[70vh]">
      <table class="w-full">
        <tbody>
          {#each lines as line, idx (idx)}
            {@const lineIssues = lineToIssues.get(idx) || []}
            {@const hasTP = lineIssues.some((i) => i.kind === "tp")}
            {@const hasFP = lineIssues.some((i) => i.kind === "fp")}
            {@const hasCritique = lineIssues.some((i) => i.kind === "critique")}
            {@const bgClass = hasTP
              ? "bg-green-50 dark:bg-green-950"
              : hasFP
                ? "bg-red-50 dark:bg-red-950"
                : hasCritique
                  ? "bg-blue-50 dark:bg-blue-950"
                  : ""}
            {@const borderClass = hasTP
              ? "border-l-4 border-green-500"
              : hasFP
                ? "border-l-4 border-red-500"
                : hasCritique
                  ? "border-l-4 border-blue-500"
                  : ""}

            <tr class="hover:bg-gray-100 dark:hover:bg-gray-800 {bgClass} {borderClass}">
              <!-- Line number (1-based display) -->
              <td
                class="px-2 py-0.5 text-right text-gray-400 dark:text-gray-500 select-none w-12 border-r dark:border-gray-700 align-top"
              >
                <div class="flex items-center justify-end gap-1">
                  {#if lineIssues.length > 0}
                    <div class="flex gap-0.5">
                      {#each lineIssues as issue (getIssueKey(issue))}
                        {#if issue.kind === "tp"}
                          <CheckCircle size={12} class="text-green-600 dark:text-green-400" />
                        {:else if issue.kind === "fp"}
                          <XCircle size={12} class="text-red-600 dark:text-red-400" />
                        {:else if issue.kind === "critique"}
                          <MessageSquare size={12} class="text-blue-600 dark:text-blue-400" />
                        {/if}
                      {/each}
                    </div>
                  {/if}
                  <span>{idx + 1}</span>
                </div>
              </td>
              <td class="px-4 py-0.5 whitespace-pre align-top">
                <!-- eslint-disable-next-line svelte/no-at-html-tags -- highlight.js output is pre-sanitized (escapes user content, adds only styling spans) -->
                {@html highlightedLines[idx] || line}
              </td>
            </tr>

            <!-- Issue comment cards (show after the first line of each issue's range) -->
            {#each lineIssues as issue (getIssueKey(issue))}
              {@const fileLocs = getLocationsForFile(issue, file.path)}
              {@const isFirstLine =
                fileLocs.length === 0 || fileLocs.every((l) => l.start_line == null)
                  ? idx === 0
                  : fileLocs.some((l) => l.start_line === idx + 1)}
              {#if isFirstLine}
                {@const issueKey = getIssueKey(issue)}
                {@const isExpanded = expandedIssues.has(issueKey)}
                {@const isTargeted = targetOccurrenceId === issue.occurrenceId}
                {@const copyUrl = issue.occurrenceId ? getOccurrenceUrl(issue.issueId, issue.occurrenceId) : undefined}
                <tr>
                  <td colspan="2" class="px-4 py-1">
                    <div
                      id={issue.occurrenceId ? `${issue.issueId}-${issue.occurrenceId}` : undefined}
                      class={isTargeted ? "ring-2 ring-blue-500 rounded" : ""}
                    >
                      <IssueComment
                        kind={issue.kind}
                        issueId={issue.occurrenceId ? `${issue.issueId}/${issue.occurrenceId}` : issue.issueId}
                        rationale={issue.rationale}
                        note={issue.note}
                        allLocations={issue.allLocations}
                        expanded={isExpanded}
                        onToggle={() => toggleIssue(issueKey)}
                        gradingEdges={issue.gradingEdges}
                        {copyUrl}
                        {snapshotSlug}
                      />
                    </div>
                  </td>
                </tr>
              {/if}
            {/each}

            <!-- Location notes (show after the last line of each location with a note) -->
            {@const locationNotes = lineToLocationNotes.get(idx) || []}
            {#each locationNotes as { loc, issue }, i (`${getIssueKey(issue)}-${loc.start_line}-${i}`)}
              <tr>
                <td colspan="2" class="px-4 py-0.5">
                  <div
                    class="text-xs italic text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 border-l-2 border-gray-300 dark:border-gray-600 px-2 py-1 rounded-r"
                  >
                    {loc.note}
                  </div>
                </td>
              </tr>
            {/each}
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>
