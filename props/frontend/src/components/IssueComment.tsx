import { UnstyledButton } from "@mantine/core";
import { CheckCircle, Link, XCircle } from "../lib/icons";
import type { GradingEdgeInfo, LocationAnchor } from "../lib/api/client";
import { issueColors } from "../lib/colors";
import { formatLocationAnchor } from "../lib/formatters";
import OccurrenceLink from "../lib/OccurrenceLink";
import CopyButton from "./CopyButton";

interface Props {
  kind: "tp" | "fp" | "critique";
  issueId: string;
  rationale: string;
  note?: string;
  allLocations?: LocationAnchor[];
  expanded?: boolean;
  onToggle?: () => void;
  gradingEdges?: GradingEdgeInfo[];
  credit?: number;
  copyUrl?: string;
  snapshotSlug?: string;
}

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

export default function IssueComment({
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
}: Props) {
  const critiqueType =
    kind !== "critique"
      ? null
      : gradingEdges.some((edge) => edge.target.kind === "tp" && edge.target.credit > 0)
        ? "tp"
        : gradingEdges.some((edge) => edge.target.kind === "fp" && edge.target.credit > 0)
          ? "fp"
          : "default";

  const Icon = kind === "tp" ? CheckCircle : kind === "fp" ? XCircle : Link;
  const styling =
    kind === "tp"
      ? { colors: issueColors.tp, label: "TP" }
      : kind === "fp"
        ? { colors: issueColors.fp, label: "FP" }
        : critiqueType === "fp"
          ? { colors: issueColors.critiqueFp, label: "Critique (FP)" }
          : { colors: issueColors.critique, label: critiqueType === "tp" ? "Critique (TP)" : "Critique" };

  return (
    <div className={`border-l-4 ${styling.colors.border} ${styling.colors.bg} rounded-r shadow-sm my-2`}>
      <UnstyledButton
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className={`w-full px-3 py-2 ${styling.colors.headerBg} flex items-center gap-2 hover:opacity-80 transition-opacity`}
      >
        <Icon size={16} className={styling.colors.text} />
        <span className="font-mono text-sm font-medium">{issueId}</span>
        <span className={`text-xs ${styling.colors.textDark} font-medium`}>{styling.label}</span>
        {credit !== undefined ? (
          <span className="text-xs text-gray-500 dark:text-gray-400">(+{credit.toFixed(2)})</span>
        ) : null}
        <span className="ml-auto text-gray-400 dark:text-gray-500 text-xs">{expanded ? "▼" : "▶"}</span>
      </UnstyledButton>

      {expanded ? (
        <div className="px-3 py-2 space-y-2 text-sm">
          {copyUrl ? (
            <div className="flex justify-end">
              <CopyButton text={copyUrl} label="Copy Link" />
            </div>
          ) : null}
          <div>
            <div className="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Rationale:</div>
            <div className="text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{rationale}</div>
          </div>

          {note ? (
            <div>
              <div className="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Note:</div>
              <div className="text-gray-700 dark:text-gray-300 italic">{note}</div>
            </div>
          ) : null}

          {allLocations.length > 1 ? (
            <div>
              <div className="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">All locations:</div>
              {allLocations.map((location, index) => (
                <div
                  key={`${location.file}-${location.start_line}-${index}`}
                  className="font-mono text-xs text-gray-700 dark:text-gray-300"
                >
                  {formatLocationAnchor(location)}
                  {location.note ? (
                    <span className="italic text-gray-500 dark:text-gray-400 ml-1">({location.note})</span>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}

          {kind === "critique" && gradingEdges.length > 0 ? (
            <div>
              <div className="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Grading:</div>
              <div className="space-y-1">
                {gradingEdges.map((edge) => {
                  const target = edge.target;
                  if (target.credit <= 0) return null;

                  const TargetIcon = target.kind === "tp" ? CheckCircle : XCircle;
                  const targetStyles = TARGET_STYLES[target.kind];
                  const targetId = target.kind === "tp" ? target.tp_id : target.fp_id;
                  const linkedTarget =
                    snapshotSlug && targetId && target.occurrence_id ? (
                      <OccurrenceLink
                        snapshotSlug={snapshotSlug}
                        issueId={targetId}
                        occurrenceId={target.occurrence_id}
                      />
                    ) : (
                      <span className={targetStyles.textColor}>
                        {targetId}/{target.occurrence_id}
                      </span>
                    );

                  return (
                    <div
                      key={`${edge.critique_issue_id}-${targetId}-${target.occurrence_id}`}
                      className={`text-xs p-1.5 rounded border ${targetStyles.bg} ${targetStyles.border}`}
                    >
                      <div className="flex items-center gap-2">
                        <TargetIcon size={12} className={targetStyles.iconColor} />
                        <span className="font-mono">{linkedTarget}</span>
                        <span className={`${targetStyles.creditColor} font-medium`}>(+{target.credit.toFixed(2)})</span>
                      </div>
                      {edge.rationale ? (
                        <div className="text-gray-600 dark:text-gray-400 mt-1">{edge.rationale}</div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
