import { Fragment, useMemo, useState } from "react";
import { Table } from "@mantine/core";
import type { DefinitionRow, SplitScopeStats, Split, ExampleKind } from "../../lib/types";
import { formatStatsWithCI, formatAge } from "../../lib/formatters";
import { recallColorClass } from "../../lib/colors";
import DefinitionIdLink from "../../lib/DefinitionIdLink";

interface CellClickInfo {
  definitionId: string;
  split: Split;
  kind: ExampleKind;
}

interface Props {
  definitions: DefinitionRow[];
  exampleCounts?: { [key: string]: { [key: string]: number } };
  onCellClick?: (info: CellClickInfo) => void;
}

function getStats(definition: DefinitionRow, split: Split, kind: ExampleKind): SplitScopeStats | undefined {
  return definition.stats[split]?.[kind];
}

const formatTableAge = (isoDate: string) => formatAge(isoDate, false);
const splits: Split[] = ["valid", "train"];
const kinds: ExampleKind[] = ["whole_snapshot", "file_set"];
const metricsPerKind = 5;

export default function DefinitionsTable({ definitions, exampleCounts, onCellClick }: Props) {
  const [sortColumn, setSortColumn] = useState("valid_whole_snapshot_recall");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("desc");

  const rows = useMemo(() => {
    const match = /^(valid|train)_(whole_snapshot|file_set)_recall$/.exec(sortColumn);
    return [...definitions].sort((left, right) => {
      let comparison = 0;
      if (sortColumn === "image_digest") {
        comparison = left.image_digest.localeCompare(right.image_digest);
      } else if (sortColumn === "created_at") {
        comparison = left.created_at.localeCompare(right.created_at);
      } else if (match) {
        const [, split, kind] = match;
        comparison =
          (getStats(left, split as Split, kind as ExampleKind)?.recall_stats?.mean ?? -1) -
          (getStats(right, split as Split, kind as ExampleKind)?.recall_stats?.mean ?? -1);
      }
      return sortDirection === "asc" ? comparison : -comparison;
    });
  }, [definitions, sortColumn, sortDirection]);

  function handleSort(columnId: string) {
    if (sortColumn === columnId) {
      setSortDirection((direction) => (direction === "asc" ? "desc" : "asc"));
    } else {
      setSortColumn(columnId);
      setSortDirection("asc");
    }
  }

  function getSortIndicator(columnId: string): string {
    if (sortColumn !== columnId) return "";
    return sortDirection === "asc" ? " ↑" : " ↓";
  }

  function getExampleCount(split: Split, kind: ExampleKind): number {
    return exampleCounts?.[split]?.[kind] ?? 0;
  }

  return (
    <div className="overflow-x-auto">
      <Table horizontalSpacing={0} verticalSpacing={0} className="min-w-full text-sm">
        <Table.Thead>
          <Table.Tr className="border-b border-gray-200 dark:border-gray-700">
            <Table.Th
              rowSpan={3}
              className="px-3 py-2 text-left cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 align-bottom"
              onClick={() => handleSort("image_digest")}
            >
              Definition{getSortIndicator("image_digest")}
            </Table.Th>
            <Table.Th
              rowSpan={3}
              className="px-3 py-2 text-right cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 align-bottom"
              onClick={() => handleSort("created_at")}
            >
              Age{getSortIndicator("created_at")}
            </Table.Th>
            {splits.map((split) => (
              <Table.Th
                key={split}
                colSpan={kinds.length * metricsPerKind}
                className="px-3 py-1 text-center border-l border-gray-300 dark:border-gray-600 font-semibold capitalize"
              >
                {split}
              </Table.Th>
            ))}
          </Table.Tr>
          <Table.Tr className="border-b border-gray-200 dark:border-gray-700">
            {splits.map((split) =>
              kinds.map((kind) => {
                const columnId = `${split}_${kind}_recall`;
                const count = getExampleCount(split, kind);
                return (
                  <Table.Th
                    key={`${split}-${kind}`}
                    colSpan={metricsPerKind}
                    className="px-2 py-1 text-center border-l border-gray-200 dark:border-gray-700 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700"
                    onClick={() => handleSort(columnId)}
                  >
                    {kind} <span className="text-gray-400 dark:text-gray-500 font-normal">(n={count})</span>
                    {getSortIndicator(columnId)}
                  </Table.Th>
                );
              })
            )}
          </Table.Tr>
          <Table.Tr className="border-b border-gray-300 dark:border-gray-600 text-xs text-gray-500 dark:text-gray-400">
            {splits.map((split) =>
              kinds.map((kind) => (
                <Fragment key={`${split}-${kind}`}>
                  <Table.Th className="px-2 py-1 text-right border-l border-gray-200 dark:border-gray-700">
                    Recall
                  </Table.Th>
                  <Table.Th className="px-2 py-1 text-right">Runs</Table.Th>
                  <Table.Th className="px-2 py-1 text-right">Zero</Table.Th>
                  <Table.Th className="px-2 py-1 text-right">Done</Table.Th>
                  <Table.Th className="px-2 py-1 text-right">Stalled</Table.Th>
                </Fragment>
              ))
            )}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((definition) => (
            <Table.Tr
              key={definition.image_digest}
              className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800"
            >
              <Table.Td className="px-3 py-2 font-mono text-xs">
                <DefinitionIdLink id={definition.image_digest} display_name={definition.display_name} />
              </Table.Td>
              <Table.Td className="px-3 py-2 text-right text-gray-600 dark:text-gray-400">
                {formatTableAge(definition.created_at)}
              </Table.Td>
              {splits.map((split) =>
                kinds.map((kind) => {
                  const stats = getStats(definition, split, kind);
                  const clickable = onCellClick != null;
                  const clickClass = clickable ? "cursor-pointer hover:bg-blue-100 dark:hover:bg-blue-900" : "";
                  const clickTitle = clickable ? `View ${split} ${kind} runs` : undefined;
                  const handleCellClick = clickable
                    ? () => onCellClick({ definitionId: definition.image_digest, split, kind })
                    : undefined;

                  if (!stats) {
                    const emptyClass = "px-2 py-2 text-right text-gray-300 dark:text-gray-600";
                    return (
                      <Fragment key={`${split}-${kind}`}>
                        <Table.Td
                          className={`${emptyClass} border-l border-gray-100 dark:border-gray-800 ${clickClass}`}
                          onClick={handleCellClick}
                          title={clickTitle}
                        >
                          —
                        </Table.Td>
                        <Table.Td className={emptyClass}>—</Table.Td>
                        <Table.Td className={emptyClass}>—</Table.Td>
                        <Table.Td className={emptyClass}>—</Table.Td>
                        <Table.Td className={emptyClass}>—</Table.Td>
                      </Fragment>
                    );
                  }

                  return (
                    <Fragment key={`${split}-${kind}`}>
                      <Table.Td
                        className={`px-2 py-2 text-right border-l border-gray-100 dark:border-gray-800 ${recallColorClass(
                          stats.recall_stats?.mean
                        )} ${clickClass}`}
                        onClick={handleCellClick}
                        title={clickTitle}
                      >
                        {stats.recall_stats ? formatStatsWithCI(stats.recall_stats) : "—"}
                      </Table.Td>
                      <Table.Td className="px-2 py-2 text-right">{stats.n_examples}</Table.Td>
                      <Table.Td className="px-2 py-2 text-right text-gray-400 dark:text-gray-500">
                        {stats.zero_count}
                      </Table.Td>
                      <Table.Td className="px-2 py-2 text-right">{stats.status_counts.completed ?? 0}</Table.Td>
                      <Table.Td className="px-2 py-2 text-right text-gray-400 dark:text-gray-500">
                        {stats.status_counts.timed_out ?? 0}
                      </Table.Td>
                    </Fragment>
                  );
                })
              )}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </div>
  );
}
