import { useMemo, useState } from "react";
import { UnstyledButton } from "@mantine/core";
import type { GradingEdgeInfo } from "../lib/schema";
import { issueColors } from "../lib/colors";
import CritiqueIssueLink from "../lib/CritiqueIssueLink";
import OccurrenceLink from "../lib/OccurrenceLink";

interface Props {
  edges: GradingEdgeInfo[];
  defaultOpen?: boolean;
  runId?: string;
  snapshotSlug?: string;
}

function edgeKey(edge: GradingEdgeInfo): string {
  const targetId = edge.target.kind === "tp" ? edge.target.tp_id : edge.target.fp_id;
  return `${edge.critique_issue_id}-${targetId}-${edge.target.occurrence_id}`;
}

export default function GradingEdges({ edges, defaultOpen = false, runId, snapshotSlug }: Props) {
  const nonZeroEdges = useMemo(
    () => edges.filter((edge) => edge.target.credit > 0).sort((a, b) => b.target.credit - a.target.credit),
    [edges]
  );
  const zeroEdges = useMemo(() => edges.filter((edge) => edge.target.credit === 0), [edges]);
  const [showZeroEdges, setShowZeroEdges] = useState(false);

  if (edges.length === 0) return null;

  return (
    <details open={defaultOpen}>
      <summary className="cursor-pointer text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 text-xs">
        Grading ({nonZeroEdges.length} matches, {zeroEdges.length} non-matches)
      </summary>
      <div className="mt-2 space-y-2">
        {nonZeroEdges.length > 0 ? (
          <div>
            <div className="text-xs font-medium mb-1">Matches ({nonZeroEdges.length}):</div>
            {nonZeroEdges.map((edge) => {
              const target = edge.target;
              const credit = target.credit;
              const isTP = target.kind === "tp";
              const colors = issueColors[target.kind];
              const targetId = isTP ? target.tp_id : target.fp_id;

              return (
                <div key={edgeKey(edge)} className={`p-2 rounded border text-xs ${colors.bg} ${colors.border}`}>
                  <div className="flex items-center gap-2 mb-1">
                    {runId ? (
                      <CritiqueIssueLink runId={runId} issueId={edge.critique_issue_id} />
                    ) : (
                      <span className="font-mono font-medium">{edge.critique_issue_id}</span>
                    )}
                    <span className="text-gray-400 dark:text-gray-500">→</span>
                    {snapshotSlug ? (
                      <span className={colors.text}>
                        <OccurrenceLink
                          snapshotSlug={snapshotSlug}
                          issueId={targetId}
                          occurrenceId={target.occurrence_id}
                        />
                      </span>
                    ) : (
                      <span className={colors.text}>
                        {targetId}/{target.occurrence_id}
                      </span>
                    )}
                    <span className={`${colors.text} font-medium`}>
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
      </div>
    </details>
  );
}
