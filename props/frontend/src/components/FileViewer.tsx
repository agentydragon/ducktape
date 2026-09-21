import { Fragment, useMemo, useState } from "react";

import { Button } from "@mantine/core";

import IssueComment from "./IssueComment";
import { CheckCircle, ChevronDown, ChevronRight, MessageSquare, XCircle } from "../lib/icons";
import { resolve } from "../lib/router";
import type {
  FileContentResponse,
  FpInfo,
  GradingEdgeInfo,
  ReportedIssueInfo,
  ReportedIssueOccurrenceInfo,
  TpInfo,
} from "../lib/api/client";
import { detectLanguage } from "../lib/fileTypes";
import { highlightLines } from "../lib/highlighting";
import type { IssueMarker, LocationAnchor } from "../lib/types";

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

/** Get locations for a specific file from an issue marker. */
function getLocationsForFile(marker: IssueMarker, filePath: string): LocationAnchor[] {
  return marker.allLocations.filter((location) => location.file === filePath);
}

/** Escape source text when syntax highlighting fails and returns its input unchanged. */
function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => {
    switch (character) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      default:
        return "&#x27;";
    }
  });
}

function getIssueKey(issue: IssueMarker): string {
  return issue.occurrenceId ? `${issue.kind}-${issue.issueId}-${issue.occurrenceId}` : `${issue.kind}-${issue.issueId}`;
}

export default function FileViewer({
  file,
  tps = [],
  fps = [],
  critiqueIssues = [],
  gradingEdges = [],
  snapshotSlug,
  targetOccurrenceId = null,
  defaultCollapsed = false,
}: Props) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const [expandedIssues, setExpandedIssues] = useState<Set<string>>(() => new Set());

  const lines = useMemo(() => {
    const raw = file.content.split("\n");
    // Remove trailing empty string when the source ends in a newline.
    if (raw.length > 1 && raw[raw.length - 1] === "") return raw.slice(0, -1);
    return raw;
  }, [file.content]);
  const language = useMemo(() => detectLanguage(file.path), [file.path]);
  const highlightedLines = useMemo(() => {
    const highlighted = highlightLines(lines, language);
    // highlightLines returns its original input on failure. Escape that fallback
    // before it is passed to dangerouslySetInnerHTML below.
    return highlighted === lines ? lines.map(escapeHtml) : highlighted;
  }, [lines, language]);

  // Combine TP, FP, and optional critique issues that refer to this file.
  const allIssues = useMemo(() => {
    const result: IssueMarker[] = [];

    for (const tp of tps) {
      for (const occurrence of tp.occurrences) {
        if (occurrence.locations.some((location) => location.file === file.path)) {
          result.push({
            kind: "tp",
            issueId: tp.tp_id,
            occurrenceId: occurrence.occurrence_id,
            rationale: tp.rationale,
            note: occurrence.note ?? undefined,
            allLocations: occurrence.locations,
          });
        }
      }
    }

    for (const fp of fps) {
      for (const occurrence of fp.occurrences) {
        if (occurrence.locations.some((location) => location.file === file.path)) {
          result.push({
            kind: "fp",
            issueId: fp.fp_id,
            occurrenceId: occurrence.occurrence_id,
            rationale: fp.rationale,
            note: occurrence.note ?? undefined,
            allLocations: occurrence.locations,
          });
        }
      }
    }

    for (const issue of critiqueIssues) {
      const issueLocations = issue.occurrences.flatMap(
        (occurrence: ReportedIssueOccurrenceInfo) => occurrence.locations
      );
      if (issueLocations.some((location) => location.file === file.path)) {
        const edges = gradingEdges.filter((edge) => edge.critique_issue_id === issue.issue_id);
        result.push({
          kind: "critique",
          issueId: issue.issue_id,
          rationale: issue.rationale,
          note: issue.occurrences[0]?.note ?? undefined,
          allLocations: issueLocations,
          gradingEdges: edges,
        });
      }
    }

    return result;
  }, [tps, fps, critiqueIssues, gradingEdges, file.path]);

  // Map zero-based line indexes to every issue covering each line.
  const lineToIssues = useMemo(() => {
    const map = new Map<number, IssueMarker[]>();

    for (const issue of allIssues) {
      const locations = getLocationsForFile(issue, file.path);
      if (locations.length === 0 || locations.every((location) => location.start_line == null)) {
        for (let index = 0; index < lines.length; index += 1) {
          map.set(index, [...(map.get(index) ?? []), issue]);
        }
      } else {
        for (const location of locations) {
          if (location.start_line == null) continue;
          const startIndex = location.start_line - 1;
          const endIndex = (location.end_line ?? location.start_line) - 1;
          for (let index = startIndex; index <= endIndex; index += 1) {
            map.set(index, [...(map.get(index) ?? []), issue]);
          }
        }
      }
    }

    return map;
  }, [allIssues, file.path, lines.length]);

  // Show each location note after the last line of its location.
  const lineToLocationNotes = useMemo(() => {
    const map = new Map<number, Array<{ issue: IssueMarker; loc: LocationAnchor }>>();

    for (const issue of allIssues) {
      for (const location of getLocationsForFile(issue, file.path)) {
        if (location.note && location.start_line != null) {
          const endIndex = (location.end_line ?? location.start_line) - 1;
          map.set(endIndex, [...(map.get(endIndex) ?? []), { issue, loc: location }]);
        }
      }
    }

    return map;
  }, [allIssues, file.path]);

  function toggleIssue(id: string) {
    setExpandedIssues((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function getOccurrenceUrl(issueId: string, occurrenceId: string): string | undefined {
    if (!snapshotSlug) return undefined;
    const routePath = `/snapshots/${snapshotSlug}/${issueId}/${occurrenceId}?file=${encodeURIComponent(file.path)}`;
    return `${window.location.origin}${resolve(routePath)}`;
  }

  const tpCount = allIssues.filter((issue) => issue.kind === "tp").length;
  const fpCount = allIssues.filter((issue) => issue.kind === "fp").length;
  const critiqueCount = allIssues.filter((issue) => issue.kind === "critique").length;
  const hasCritiques = critiqueIssues.length > 0;

  return (
    <div className="border dark:border-gray-700 rounded bg-white dark:bg-gray-900 font-mono text-sm">
      <Button
        unstyled
        type="button"
        className={`px-4 py-2 bg-gray-50 dark:bg-gray-800 flex items-center gap-2 w-full text-left cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 ${
          collapsed ? "" : "border-b dark:border-gray-700"
        }`}
        onClick={() => setCollapsed((value) => !value)}
        aria-expanded={!collapsed}
      >
        {collapsed ? (
          <ChevronRight size={16} className="text-gray-500 dark:text-gray-400 flex-shrink-0" />
        ) : (
          <ChevronDown size={16} className="text-gray-500 dark:text-gray-400 flex-shrink-0" />
        )}
        <span className="font-semibold">{file.path}</span>
        <span className="text-gray-500 dark:text-gray-400 text-xs">({file.line_count} lines)</span>
        <span className="text-gray-500 dark:text-gray-400 text-xs ml-auto">
          {hasCritiques && <>{critiqueCount} critique, </>}
          {tpCount} TPs, {fpCount} FPs
        </span>
      </Button>

      {!collapsed && (
        <div className="overflow-auto max-h-[70vh]">
          <table className="w-full">
            <tbody>
              {lines.map((line, index) => {
                const lineIssues = lineToIssues.get(index) ?? [];
                const hasTP = lineIssues.some((issue) => issue.kind === "tp");
                const hasFP = lineIssues.some((issue) => issue.kind === "fp");
                const hasCritique = lineIssues.some((issue) => issue.kind === "critique");
                const bgClass = hasTP
                  ? "bg-green-50 dark:bg-green-950"
                  : hasFP
                    ? "bg-red-50 dark:bg-red-950"
                    : hasCritique
                      ? "bg-blue-50 dark:bg-blue-950"
                      : "";
                const borderClass = hasTP
                  ? "border-l-4 border-green-500"
                  : hasFP
                    ? "border-l-4 border-red-500"
                    : hasCritique
                      ? "border-l-4 border-blue-500"
                      : "";

                return (
                  <Fragment key={index}>
                    <tr className={`hover:bg-gray-100 dark:hover:bg-gray-800 ${bgClass} ${borderClass}`}>
                      <td className="px-2 py-0.5 text-right text-gray-400 dark:text-gray-500 select-none w-12 border-r dark:border-gray-700 align-top">
                        <div className="flex items-center justify-end gap-1">
                          {lineIssues.length > 0 && (
                            <div className="flex gap-0.5">
                              {lineIssues.map((issue, issueIndex) => (
                                <Fragment key={`${getIssueKey(issue)}-${issueIndex}`}>
                                  {issue.kind === "tp" ? (
                                    <CheckCircle size={12} className="text-green-600 dark:text-green-400" />
                                  ) : issue.kind === "fp" ? (
                                    <XCircle size={12} className="text-red-600 dark:text-red-400" />
                                  ) : (
                                    <MessageSquare size={12} className="text-blue-600 dark:text-blue-400" />
                                  )}
                                </Fragment>
                              ))}
                            </div>
                          )}
                          <span>{index + 1}</span>
                        </div>
                      </td>
                      <td className="px-4 py-0.5 whitespace-pre align-top">
                        {/* highlight.js escapes source text and emits its own span markup. The failure fallback is escaped above. */}
                        <span dangerouslySetInnerHTML={{ __html: highlightedLines[index] ?? escapeHtml(line) }} />
                      </td>
                    </tr>

                    {lineIssues.map((issue, issueIndex) => {
                      const fileLocations = getLocationsForFile(issue, file.path);
                      const isFirstLine =
                        fileLocations.length === 0 || fileLocations.every((location) => location.start_line == null)
                          ? index === 0
                          : fileLocations.some((location) => location.start_line === index + 1);
                      if (!isFirstLine) return null;

                      const issueKey = getIssueKey(issue);
                      const copyUrl = issue.occurrenceId
                        ? getOccurrenceUrl(issue.issueId, issue.occurrenceId)
                        : undefined;
                      return (
                        <tr key={`${issueKey}-${issueIndex}`}>
                          <td colSpan={2} className="px-4 py-1">
                            <div
                              id={issue.occurrenceId ? `${issue.issueId}-${issue.occurrenceId}` : undefined}
                              className={
                                targetOccurrenceId === issue.occurrenceId ? "ring-2 ring-blue-500 rounded" : ""
                              }
                            >
                              <IssueComment
                                kind={issue.kind}
                                issueId={issue.occurrenceId ? `${issue.issueId}/${issue.occurrenceId}` : issue.issueId}
                                rationale={issue.rationale}
                                note={issue.note}
                                allLocations={issue.allLocations}
                                expanded={expandedIssues.has(issueKey)}
                                onToggle={() => toggleIssue(issueKey)}
                                gradingEdges={issue.gradingEdges}
                                copyUrl={copyUrl}
                                snapshotSlug={snapshotSlug}
                              />
                            </div>
                          </td>
                        </tr>
                      );
                    })}

                    {(lineToLocationNotes.get(index) ?? []).map(({ loc, issue }, noteIndex) => (
                      <tr key={`${getIssueKey(issue)}-${loc.start_line}-${noteIndex}`}>
                        <td colSpan={2} className="px-4 py-0.5">
                          <div className="text-xs italic text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 border-l-2 border-gray-300 dark:border-gray-600 px-2 py-1 rounded-r">
                            {loc.note}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
