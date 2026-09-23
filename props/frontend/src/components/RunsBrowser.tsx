import { Badge, Button, Group, Paper, Select, Table, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from "react";

import {
  AGENT_RUN_STATUS_VALUES,
  AGENT_TYPE_VALUES,
  fetchRuns,
  type AgentRunStatus,
  type AgentType,
  type CriticTypeConfig,
  type ExampleKind,
  type RunInfo,
  type RunsFilters,
  type Split,
} from "../lib/api/client";
import { formatAge } from "../lib/formatters";
import { formatStatus } from "../lib/status";
import { toast } from "../lib/toast";
import type { RunTrigger } from "../lib/types";
import BackButton from "./BackButton";
import DefinitionIdLink from "../lib/DefinitionIdLink";
import ExampleLink from "../lib/ExampleLink";
import RunIdLink from "../lib/RunIdLink";

interface Props {
  initialDefinitionId?: string;
  initialSplit?: Split;
  initialKind?: ExampleKind;
  onTriggerRun?: (run: RunTrigger) => void;
  initialRuns?: RunInfo[];
  initialTotalCount?: number;
}

const LIMIT = 50;

function sortValue(run: RunInfo, column: string): number | string {
  switch (column) {
    case "agent_run_id":
      return run.agent_run_id;
    case "image_digest":
      return run.image_digest;
    case "split":
      return run.split ?? "";
    case "model":
      return run.model;
    case "status":
      return run.status;
    case "created_at":
      return Date.parse(run.created_at);
    default:
      return "";
  }
}

function statusColor(status: AgentRunStatus): string {
  switch (status) {
    case "in_progress":
      return "blue";
    case "exited":
      return "green";
    case "timed_out":
      return "yellow";
    default:
      return "gray";
  }
}

export default function RunsBrowser({
  initialDefinitionId,
  initialSplit,
  initialKind,
  onTriggerRun,
  initialRuns,
  initialTotalCount,
}: Props): JSX.Element {
  const [runs, setRuns] = useState<RunInfo[]>(initialRuns ?? []);
  const [totalCount, setTotalCount] = useState(initialTotalCount ?? 0);
  const [loading, setLoading] = useState(!initialRuns);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState<AgentRunStatus | "">("");
  const [agentTypeFilter, setAgentTypeFilter] = useState<AgentType | "">("");
  const [sortColumn, setSortColumn] = useState("created_at");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("desc");
  const skipInitialFetch = useRef(Boolean(initialRuns));

  const filters = useMemo<RunsFilters>(() => {
    const result: RunsFilters = { offset, limit: LIMIT };
    if (statusFilter) result.status = statusFilter;
    if (agentTypeFilter) result.agent_type = agentTypeFilter;
    if (initialDefinitionId) result.image_digest = initialDefinitionId;
    if (initialSplit) result.split = initialSplit;
    if (initialKind) result.example_kind = initialKind;
    return result;
  }, [offset, statusFilter, agentTypeFilter, initialDefinitionId, initialSplit, initialKind]);

  const loadRuns = useCallback(async () => {
    setLoading(true);
    try {
      const result = await fetchRuns(filters);
      setRuns(result?.runs ?? []);
      setTotalCount(result?.total_count ?? 0);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Unknown error");
      setRuns([]);
      setTotalCount(0);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    if (skipInitialFetch.current) {
      skipInitialFetch.current = false;
      return;
    }
    void loadRuns();
  }, [loadRuns]);

  const sortedRuns = useMemo(() => {
    return [...runs].sort((left, right) => {
      const leftValue = sortValue(left, sortColumn);
      const rightValue = sortValue(right, sortColumn);
      const comparison =
        typeof leftValue === "number" && typeof rightValue === "number"
          ? leftValue - rightValue
          : String(leftValue).localeCompare(String(rightValue));
      return sortDirection === "asc" ? comparison : -comparison;
    });
  }, [runs, sortColumn, sortDirection]);

  function handleSort(column: string): void {
    if (sortColumn === column) setSortDirection((direction) => (direction === "asc" ? "desc" : "asc"));
    else {
      setSortColumn(column);
      setSortDirection("asc");
    }
  }

  function indicator(column: string): string {
    return sortColumn !== column ? "" : sortDirection === "asc" ? " ↑" : " ↓";
  }

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + LIMIT, totalCount);
  const sortableHeader = (column: string, label: string, align: "left" | "right" = "left") => (
    <Table.Th
      key={column}
      ta={align}
      onClick={() => handleSort(column)}
      className="cursor-pointer select-none hover:bg-gray-100 dark:hover:bg-gray-700"
    >
      {label}
      {indicator(column)}
    </Table.Th>
  );

  return (
    <Paper shadow="sm" p="md" className="dark:bg-gray-900">
      <Group mb="md">
        <BackButton />
        <Title order={3} m={0}>
          {initialDefinitionId ? (
            <>
              Runs for <span className="font-mono text-blue-600 dark:text-blue-400">{initialDefinitionId}</span>
              {initialSplit && (
                <Text span c="dimmed">
                  {" "}
                  / {initialSplit}
                </Text>
              )}
              {initialKind && (
                <Text span c="dimmed">
                  {" "}
                  / {initialKind}
                </Text>
              )}
            </>
          ) : (
            "All Runs"
          )}
        </Title>
        {onTriggerRun && initialDefinitionId && initialSplit && initialKind && (
          <Button
            ml="auto"
            onClick={() =>
              onTriggerRun({
                definitionId: initialDefinitionId,
                split: initialSplit,
                kind: initialKind,
              })
            }
          >
            + New Run
          </Button>
        )}
      </Group>

      <Group align="end" mb="md">
        <Select
          label="Status"
          placeholder="All"
          data={AGENT_RUN_STATUS_VALUES.map((value) => ({ value, label: formatStatus(value) }))}
          value={statusFilter || null}
          onChange={(value) => {
            setStatusFilter((value as AgentRunStatus | null) ?? "");
            setOffset(0);
          }}
          clearable
          w={170}
        />
        <Select
          label="Agent Type"
          placeholder="All"
          data={AGENT_TYPE_VALUES.map((value) => ({ value, label: value }))}
          value={agentTypeFilter || null}
          onChange={(value) => {
            setAgentTypeFilter((value as AgentType | null) ?? "");
            setOffset(0);
          }}
          clearable
          w={220}
        />
        <Text ml="auto" size="sm" c="dimmed">
          {pageStart}–{pageEnd} of {totalCount}
        </Text>
      </Group>

      {loading ? (
        <Text size="sm" c="dimmed">
          Loading…
        </Text>
      ) : runs.length === 0 ? (
        <Text size="sm" c="dimmed">
          No runs found
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={1450}>
          <Table striped highlightOnHover withTableBorder>
            <Table.Thead>
              <Table.Tr>
                {sortableHeader("agent_run_id", "ID")}
                {sortableHeader("image_digest", "Definition")}
                {sortableHeader("split", "Split")}
                <Table.Th>Example</Table.Th>
                {sortableHeader("model", "Model")}
                {sortableHeader("status", "Status")}
                <Table.Th ta="right">Issues</Table.Th>
                <Table.Th ta="right">LLM Reqs</Table.Th>
                <Table.Th>Grading</Table.Th>
                <Table.Th ta="right" title="Present grading edges / Drift (pending) grading edges">
                  Present/Drift
                </Table.Th>
                <Table.Th ta="right">TPs</Table.Th>
                <Table.Th ta="right">FPs</Table.Th>
                <Table.Th ta="right">Credit</Table.Th>
                {sortableHeader("created_at", "Created")}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {sortedRuns.map((run) => {
                const config = run.type_config.agent_type === "critic" ? (run.type_config as CriticTypeConfig) : null;
                const grading = run.grading;
                const fullyGraded = grading != null && grading.drift_edges === 0;
                const partiallyGraded = grading != null && grading.present_edges > 0;
                return (
                  <Table.Tr key={run.agent_run_id}>
                    <Table.Td className="text-xs">
                      <RunIdLink id={run.agent_run_id} />
                    </Table.Td>
                    <Table.Td className="text-xs">
                      <DefinitionIdLink id={run.image_digest} />
                    </Table.Td>
                    <Table.Td c="dimmed" className="text-xs">
                      {run.split ?? "—"}
                    </Table.Td>
                    <Table.Td c="dimmed" className="text-xs">
                      {config ? <ExampleLink example={config.example} /> : "—"}
                    </Table.Td>
                    <Table.Td c="dimmed" className="text-xs">
                      {run.model}
                    </Table.Td>
                    <Table.Td>
                      <Badge color={statusColor(run.status)} variant="light" tt="capitalize">
                        {formatStatus(run.status)}
                        {run.status === "exited" && run.container_exit_code != null
                          ? ` (${run.container_exit_code})`
                          : ""}
                      </Badge>
                    </Table.Td>
                    <Table.Td ta="right" c="dimmed" className="text-xs">
                      {run.reported_issues_count ?? "—"}
                    </Table.Td>
                    <Table.Td ta="right" c="dimmed" className="text-xs">
                      {run.llm_requests_count ?? "—"}
                    </Table.Td>
                    <Table.Td>
                      {grading ? (
                        <Badge color={fullyGraded ? "green" : partiallyGraded ? "yellow" : "gray"} variant="light">
                          {fullyGraded ? "Graded" : partiallyGraded ? "Partial" : "None"}
                        </Badge>
                      ) : (
                        <Text c="dimmed">—</Text>
                      )}
                    </Table.Td>
                    <Table.Td ta="right" c="dimmed" className="text-xs">
                      {grading ? (
                        <>
                          {grading.present_edges}
                          <span className="text-gray-400">/{grading.drift_edges}</span>
                        </>
                      ) : (
                        "—"
                      )}
                    </Table.Td>
                    <Table.Td ta="right" c="dimmed" className="text-xs">
                      {grading?.tp_count ?? "—"}
                    </Table.Td>
                    <Table.Td ta="right" c="dimmed" className="text-xs">
                      {grading?.fp_count ?? "—"}
                    </Table.Td>
                    <Table.Td ta="right" c="dimmed" className="text-xs">
                      {grading ? grading.total_credit.toFixed(2) : "—"}
                    </Table.Td>
                    <Table.Td c="dimmed" className="text-xs">
                      {formatAge(run.created_at)}
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}

      {!loading && runs.length > 0 && (
        <Group justify="space-between" mt="md">
          <Button
            variant="default"
            onClick={() => setOffset((current) => Math.max(0, current - LIMIT))}
            disabled={offset === 0}
          >
            ← Previous
          </Button>
          <Button
            variant="default"
            onClick={() => setOffset((current) => current + LIMIT)}
            disabled={offset + LIMIT >= totalCount}
          >
            Next →
          </Button>
        </Group>
      )}
    </Paper>
  );
}
