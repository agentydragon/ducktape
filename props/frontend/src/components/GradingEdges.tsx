import { useMemo, useState } from "react";
import { UnstyledButton } from "@mantine/core";
import type { GradingEdgeInfo } from "../lib/api/client";
import CritiqueIssueLink from "../lib/CritiqueIssueLink";
import OccurrenceLink from "../lib/OccurrenceLink";

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
  runId?: string;
  snapshotSlug?: string;
}

function edgeKey(edge: GradingEdgeInfo): string {
  const targetId = edge.target.kind === "tp" ? edge.target.tp_id : edge.target.fp_id;
  return `${edge.critique_issue_id}-${targetId}-${edge.target.occurrence_id}`;
}

export default function GradingEdges({
  edges,
  missedOccurrences = [],
  totalCredit,
  recallDenominator,
  defaultOpen = false,
  runId,
  snapshotSlug,
}: Props) {
  const creditSummary =
    totalCredit != null && recallDenominator != null ? `${totalCredit.toFixed(1)}/${recallDenominator} recall` : null;
  const nonZeroEdges = useMemo(
    () => edges.filter((edge) => edge.target.credit > 0).sort((a, b) => b.target.credit - a.target.credit),
    [edges]
  );
  const zeroEdges = useMemo(() => edges.filter((edge) => edge.target.credit === 0), [edges]);
  const [showZeroEdges, setShowZeroEdges] = useState(false);

  if (edges.length === 0 && missedOccurrences.length === 0) return null;

  return (
    <details open={defaultOpen}>
      <summary className="cursor-pointer text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 text-xs">
        Grading ({nonZeroEdges.length} matches, {zeroEdges.length} non-matches)
        {creditSummary ? <span className="text-gray-400 dark:text-gray-500"> — {creditSummary}</span> : null}
      </summary>
      <div className="mt-2 space-y-2">
        {nonZeroEdges.length > 0 ? (
          <div>
            <div className="text-xs font-medium mb-1">Matches ({nonZeroEdges.length}):</div>
            {nonZeroEdges.map((edge) => {
              const target = edge.target;
              const credit = target.credit;
              const isTP = target.kind === "tp";
              const bgColor = isTP ? "bg-green-50 dark:bg-green-950" : "bg-red-50 dark:bg-red-950";
              const borderColor = isTP
                ? "border-green-200 dark:border-green-800"
                : "border-red-200 dark:border-red-800";
              const textColor = isTP ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400";
              const targetId = isTP ? target.tp_id : target.fp_id;

              return (
                <div key={edgeKey(edge)} className={`p-2 rounded border text-xs ${bgColor} ${borderColor}`}>
                  <div className="flex items-center gap-2 mb-1">
                    {runId ? (
                      <CritiqueIssueLink runId={runId} issueId={edge.critique_issue_id} />
                    ) : (
                      <span className="font-mono font-medium">{edge.critique_issue_id}</span>
                    )}
                    <span className="text-gray-400 dark:text-gray-500">→</span>
                    {snapshotSlug ? (
                      <span className={textColor}>
                        <OccurrenceLink
                          snapshotSlug={snapshotSlug}
                          issueId={targetId}
                          occurrenceId={target.occurrence_id}
                        />
                      </span>
                    ) : (
                      <span className={textColor}>
                        {targetId}/{target.occurrence_id}
                      </span>
                    )}
                    <span className={`${textColor} font-medium`}>
                      (+{credit.toFixed(2)}
                      {isTP ? "" : " FP"})
                    </span>
                  </div>
                  <div className="text-gray-600 dark:text-gray-400">{edge.rationale}</div>
                </div>
              );
            })}
          </div>
        ) : null}

        {zeroEdges.length > 0 ? (
          <div className="mt-3 pt-2 border-t border-gray-200 dark:border-gray-700">
            <UnstyledButton
              type="button"
              aria-expanded={showZeroEdges}
              onClick={() => setShowZeroEdges((show) => !show)}
              className="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 flex items-center gap-1"
            >
              <span>{showZeroEdges ? "▼" : "▶"}</span>
              <span>Non-matches ({zeroEdges.length})</span>
            </UnstyledButton>
            {showZeroEdges ? (
              <div className="mt-2 space-y-1">
                {zeroEdges.map((edge) => {
                  const target = edge.target;
                  const targetId = target.kind === "tp" ? target.tp_id : target.fp_id;
                  return (
                    <div
                      key={edgeKey(edge)}
                      className="p-2 rounded border text-xs bg-gray-50 dark:bg-gray-800 border-gray-200 dark:border-gray-700"
                    >
                      <div className="flex items-center gap-2 mb-1">
                        {runId ? (
                          <span className="text-gray-500 dark:text-gray-400">
                            <CritiqueIssueLink runId={runId} issueId={edge.critique_issue_id} />
                          </span>
                        ) : (
                          <span className="font-mono text-gray-500 dark:text-gray-400">{edge.critique_issue_id}</span>
                        )}
                        <span className="text-gray-400 dark:text-gray-500">→</span>
                        <span className="text-gray-500 dark:text-gray-400">
                          {snapshotSlug ? (
                            <OccurrenceLink
                              snapshotSlug={snapshotSlug}
                              issueId={targetId}
                              occurrenceId={target.occurrence_id}
                            />
                          ) : (
                            <>
                              {targetId}/{target.occurrence_id}
                            </>
                          )}
                          {target.kind === "fp" ? " FP" : null}
                        </span>
                        <span className="text-gray-400 dark:text-gray-500">(0.00)</span>
                      </div>
                      <div className="text-gray-500 dark:text-gray-400 text-[11px]">{edge.rationale}</div>
                    </div>
                  );
                })}
              </div>
            ) : null}
          </div>
        ) : null}

        {missedOccurrences.length > 0 ? (
          <div className="mt-3 pt-2 border-t border-gray-200 dark:border-gray-700">
            <div className="text-xs font-medium text-red-600 dark:text-red-400 mb-1">
              Missed ({missedOccurrences.length}):
            </div>
            {missedOccurrences.map((missed) => (
              <div
                key={`${missed.tp_id}-${missed.occurrence_id}`}
                className="p-2 rounded border text-xs bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800"
              >
                <div className="flex items-center gap-2">
                  {snapshotSlug ? (
                    <span className="font-mono font-medium text-red-700 dark:text-red-300">
                      <OccurrenceLink
                        snapshotSlug={snapshotSlug}
                        issueId={missed.tp_id}
                        occurrenceId={missed.occurrence_id}
                      />
                    </span>
                  ) : (
                    <span className="font-mono font-medium text-red-700 dark:text-red-300">
                      {missed.tp_id}/{missed.occurrence_id}
                    </span>
                  )}
                </div>
                <div className="text-gray-600 dark:text-gray-400 mt-1">{missed.tp_rationale}</div>
                {missed.occ_note ? (
                  <div className="text-gray-500 dark:text-gray-400 italic mt-1">{missed.occ_note}</div>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </details>
  );
}
