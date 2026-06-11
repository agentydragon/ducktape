<script lang="ts">
  import "highlight.js/styles/github.css";
  import { resolve } from "$lib/router";
  import { SvelteMap, SvelteSet } from "svelte/reactivity";
  import { CheckCircle, XCircle, MessageSquare } from "lucide-svelte";
  import type {
    FileContentResponse,
    TpInfo,
    FpInfo,
    GradingEdgeInfo,
    ReportedIssueInfo,
    ReportedIssueOccurrenceInfo,
  } from "../lib/api/client";
  import type { IssueMarker, LineRange } from "../lib/types";
  import IssueComment from "./IssueComment.svelte";
  import { detectLanguage } from "../lib/fileTypes";
  import { highlightLines } from "../lib/highlighting";

  function getRangesForFile(marker: IssueMarker, filePath: string): LineRange[] | null {
    return marker.allFiles.find((f) => f.path === filePath)?.ranges ?? null;
  }

  interface Props {
    file: FileContentResponse;
    tps?: TpInfo[];
    fps?: FpInfo[];
    critiqueIssues?: ReportedIssueInfo[];
    gradingEdges?: GradingEdgeInfo[];
    snapshotSlug?: string;
    targetOccurrenceId?: string | null;
  }

  let {
    file,
    tps = [],
    fps = [],
    critiqueIssues = [],
    gradingEdges = [],
    snapshotSlug,
    targetOccurrenceId = null,
  }: Props = $props();

  const lines = $derived(file.content.split("\n"));
  const language = $derived(detectLanguage(file.path));
  const highlightedLines = $derived(highlightLines(lines, language));

  // Combine all issues (TPs, FPs, and optionally critique issues) that reference this file
  const allIssues = $derived.by<IssueMarker[]>(() => {
    const result: IssueMarker[] = [];

    for (const tp of tps) {
      for (const occ of tp.occurrences) {
        if (occ.files.some((f) => f.path === file.path)) {
          result.push({
            kind: "tp",
            issueId: tp.tp_id,
            occurrenceId: occ.occurrence_id,
            rationale: tp.rationale,
            note: occ.note ?? undefined,
            allFiles: occ.files,
          });
        }
      }
    }

    for (const fp of fps) {
      for (const occ of fp.occurrences) {
        if (occ.files.some((f) => f.path === file.path)) {
          result.push({
            kind: "fp",
            issueId: fp.fp_id,
            occurrenceId: occ.occurrence_id,
            rationale: fp.rationale,
            note: occ.note ?? undefined,
            allFiles: occ.files,
          });
        }
      }
    }

    for (const issue of critiqueIssues) {
      const issueAllFiles = issue.occurrences.flatMap((occ: ReportedIssueOccurrenceInfo) => occ.files);
      if (issueAllFiles.some((f) => f.path === file.path)) {
        const edges = gradingEdges.filter((e) => e.critique_issue_id === issue.issue_id);
        const note = issue.occurrences[0]?.note ?? undefined;
        result.push({
          kind: "critique",
          issueId: issue.issue_id,
          rationale: issue.rationale,
          note,
          allFiles: issueAllFiles,
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
      const ranges = getRangesForFile(issue, file.path);
      if (!ranges) {
        for (let i = 0; i < lines.length; i++) {
          const existing = map.get(i) || [];
          map.set(i, [...existing, issue]);
        }
      } else {
        for (const range of ranges) {
          const startIdx = range.start_line;
          const endIdx = range.end_line ?? range.start_line;
          for (let i = startIdx; i <= endIdx; i++) {
            const existing = map.get(i) || [];
            map.set(i, [...existing, issue]);
          }
        }
      }
    }

    return map;
  });

  // Map line numbers to range notes (for ranges that end on that line)
  const lineToRangeNotes = $derived.by<SvelteMap<number, Array<{ issue: IssueMarker; range: LineRange }>>>(() => {
    const map = new SvelteMap<number, Array<{ issue: IssueMarker; range: LineRange }>>();

    for (const issue of allIssues) {
      const ranges = getRangesForFile(issue, file.path);
      if (ranges) {
        for (const range of ranges) {
          if (range.note) {
            const endIdx = range.end_line ?? range.start_line;
            const existing = map.get(endIdx) || [];
            map.set(endIdx, [...existing, { issue, range }]);
          }
        }
      }
    }

    return map;
  });

  let expandedIssues = $state(new SvelteSet<string>());

  function toggleIssue(id: string) {
    const newSet = new SvelteSet(expandedIssues);
    if (newSet.has(id)) {
      newSet.delete(id);
    } else {
      newSet.add(id);
    }
    expandedIssues = newSet;
  }

  function getIssueKey(issue: IssueMarker): string {
    return issue.occurrenceId
      ? `${issue.kind}-${issue.issueId}-${issue.occurrenceId}`
      : `${issue.kind}-${issue.issueId}`;
  }

  function getOccurrenceUrl(issueId: string, occurrenceId: string): string | undefined {
    if (!snapshotSlug) return undefined;
    const routePath = `/snapshots/${snapshotSlug}/${issueId}/${occurrenceId}?file=${encodeURIComponent(file.path)}`;
    return `${window.location.origin}/${resolve(routePath)}`;
  }

  const tpCount = $derived(allIssues.filter((i) => i.kind === "tp").length);
  const fpCount = $derived(allIssues.filter((i) => i.kind === "fp").length);
  const critiqueCount = $derived(allIssues.filter((i) => i.kind === "critique").length);
  const hasCritiques = $derived(critiqueIssues.length > 0);
</script>

<div class="border rounded bg-white font-mono text-sm">
  <!-- Header -->
  <div class="px-4 py-2 border-b bg-gray-50 flex items-center gap-2">
    <span class="font-semibold">{file.path}</span>
    <span class="text-gray-500 text-xs">({file.line_count} lines)</span>
    <span class="text-gray-500 text-xs ml-auto">
      {#if hasCritiques}
        {critiqueCount} critique,
      {/if}
      {tpCount} TPs,
      {fpCount} FPs
    </span>
  </div>

  <!-- Content -->
  <div class="overflow-auto max-h-[70vh]">
    <table class="w-full">
      <tbody>
        {#each lines as line, idx (idx)}
          {@const lineIssues = lineToIssues.get(idx) || []}
          {@const hasTP = lineIssues.some((i) => i.kind === "tp")}
          {@const hasFP = lineIssues.some((i) => i.kind === "fp")}
          {@const hasCritique = lineIssues.some((i) => i.kind === "critique")}
          {@const bgClass = hasTP ? "bg-green-50" : hasFP ? "bg-red-50" : hasCritique ? "bg-blue-50" : ""}
          {@const borderClass = hasTP
            ? "border-l-4 border-green-500"
            : hasFP
              ? "border-l-4 border-red-500"
              : hasCritique
                ? "border-l-4 border-blue-500"
                : ""}

          <tr class="hover:bg-gray-100 {bgClass} {borderClass}">
            <!-- Line number (1-based display) -->
            <td class="px-2 py-0.5 text-right text-gray-400 select-none w-12 border-r align-top">
              <div class="flex items-center justify-end gap-1">
                {#if lineIssues.length > 0}
                  <div class="flex gap-0.5">
                    {#each lineIssues as issue (getIssueKey(issue))}
                      {#if issue.kind === "tp"}
                        <CheckCircle size={12} class="text-green-600" />
                      {:else if issue.kind === "fp"}
                        <XCircle size={12} class="text-red-600" />
                      {:else if issue.kind === "critique"}
                        <MessageSquare size={12} class="text-blue-600" />
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
            {@const issueRanges = getRangesForFile(issue, file.path)}
            {@const isFirstLine =
              !issueRanges || issueRanges.length === 0 ? idx === 0 : issueRanges.some((r) => r.start_line === idx)}
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
                      allFiles={issue.allFiles}
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

          <!-- Range notes (show after the last line of each range with a note) -->
          {@const rangeNotes = lineToRangeNotes.get(idx) || []}
          {#each rangeNotes as { range, issue } (`${getIssueKey(issue)}-${range.start_line}`)}
            <tr>
              <td colspan="2" class="px-4 py-0.5">
                <div class="text-xs italic text-gray-600 bg-gray-50 border-l-2 border-gray-300 px-2 py-1 rounded-r">
                  {range.note}
                </div>
              </td>
            </tr>
          {/each}
        {/each}
      </tbody>
    </table>
  </div>
</div>
