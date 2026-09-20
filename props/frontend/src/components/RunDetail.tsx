import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@mantine/core";
import { toast } from "../lib/toast";
import BackButton from "./BackButton";
import Breadcrumb from "./Breadcrumb";
import LLMRequestViewer from "./LLMRequestViewer";
import {
  fetchRun,
  fetchSnapshotDetail,
  fetchSnapshotFiles,
  fetchLLMRequests,
  fetchRunLogs,
  type AgentRunDetail,
  type CriticTypeConfig,
  type GraderTypeConfig,
  type CriticDevImproveTypeConfig,
  type CriticDevOptimizeTypeConfig,
  type SnapshotDetailResponse,
  type FileContentResponse,
  type GradingEdgeInfo,
  type ReportedIssueInfo,
  type LLMRequestInfo,
  isCriticRun,
  isGraderRun,
} from "../lib/api/client";
import { getStatusColor, formatStatus } from "../lib/status";
import RunIdLink from "../lib/RunIdLink";
import DefinitionIdLink from "../lib/DefinitionIdLink";
import ExampleLink from "../lib/ExampleLink";
import GradingEdges from "./GradingEdges";
import FileViewer from "./FileViewer";

// Props also support visual tests that seed the component with already loaded data.
interface Props {
  runId: string;
  initialRun?: AgentRunDetail;
  initialSnapshotDetail?: SnapshotDetailResponse;
  initialFileContents?: Map<string, FileContentResponse>;
  initialLLMRequests?: LLMRequestInfo[];
}

type RunLoad = { status: "loading" } | { status: "loaded"; run: AgentRunDetail } | { status: "error"; message: string };

type LogTab = "logs" | "llm";

function getAgentType(run: AgentRunDetail): string {
  return "agent_type" in run.details ? run.details.agent_type : "unknown";
}

function getReportedIssues(run: AgentRunDetail): ReportedIssueInfo[] {
  if (!isCriticRun(run)) return [];
  return run.details.reported_issues;
}

function getGradingEdges(run: AgentRunDetail): GradingEdgeInfo[] {
  if (!isGraderRun(run)) return [];
  return run.details.grading_edges;
}

function getResolvedFiles(run: AgentRunDetail): string[] | null {
  if (!isCriticRun(run)) return null;
  return run.details.resolved_files;
}

function getSnapshotSlug(run: AgentRunDetail): string | undefined {
  const config = run.type_config;
  if ("example" in config && config.example) {
    return config.example.snapshot_slug;
  }
  return undefined;
}

function getAggregatedEdges(run: AgentRunDetail): GradingEdgeInfo[] {
  if (!isCriticRun(run)) return [];
  return run.details.grader_runs.flatMap((graderRun) => graderRun.grading_edges);
}

function computeGradingSummary(run: AgentRunDetail) {
  if (!isCriticRun(run)) return null;

  const edges = getAggregatedEdges(run);
  if (edges.length === 0) return null;

  const tp_count = edges.filter((edge) => edge.target.kind === "tp" && edge.target.credit > 0).length;
  const fp_count = edges.filter((edge) => edge.target.kind === "fp" && edge.target.credit > 0).length;
  const total_credit = edges
    .filter((edge) => edge.target.kind === "tp")
    .reduce((sum, edge) => sum + edge.target.credit, 0);

  return { tp_count, fp_count, total_credit };
}

export default function RunDetail({
  runId,
  initialRun,
  initialSnapshotDetail,
  initialFileContents,
  initialLLMRequests,
}: Props) {
  // initialRun seeds the loaded state for visual tests and server-rendered data.
  const [load, setLoad] = useState<RunLoad>(() =>
    initialRun ? { status: "loaded", run: initialRun } : { status: "loading" }
  );

  const [snapshotDetail, setSnapshotDetail] = useState<SnapshotDetailResponse | null>(initialSnapshotDetail ?? null);
  const [fileContents, setFileContents] = useState<Map<string, FileContentResponse>>(
    () => new Map(initialFileContents ?? [])
  );
  const [loadingSnapshot, setLoadingSnapshot] = useState(false);
  const [llmRequests, setLlmRequests] = useState<LLMRequestInfo[]>(initialLLMRequests ?? []);
  const [loadingLLMRequests, setLoadingLLMRequests] = useState(false);
  const [containerLogs, setContainerLogs] = useState<string | null>(null);
  const [loadingLogs, setLoadingLogs] = useState(false);
  const [activeLogTab, setActiveLogTab] = useState<LogTab>("llm");

  // Refs keep the lazy-load guard synchronous between the first request and React's next render.
  const snapshotDetailRef = useRef(snapshotDetail);
  snapshotDetailRef.current = snapshotDetail;
  const loadingSnapshotRef = useRef(loadingSnapshot);
  loadingSnapshotRef.current = loadingSnapshot;
  const loadingLLMRequestsRef = useRef(false);
  const loadingLogsRef = useRef(false);

  const loadSnapshotData = useCallback(async (criticRun: AgentRunDetail) => {
    if (getAgentType(criticRun) !== "critic") return;

    const config = criticRun.type_config as CriticTypeConfig;
    const snapshotSlug = config.example.snapshot_slug;

    loadingSnapshotRef.current = true;
    setLoadingSnapshot(true);
    try {
      // Fetch snapshot detail filtered to this example's recall scope.
      const exampleKind = config.example.kind;
      const filesHash = exampleKind === "file_set" ? config.example.files_hash : undefined;
      const loadedSnapshotDetail = await fetchSnapshotDetail(snapshotSlug, exampleKind, filesHash);
      setSnapshotDetail(loadedSnapshotDetail);

      // Collect files mentioned in critique issues or ground truth.
      const allFilePaths = new Set<string>();

      for (const issue of getReportedIssues(criticRun)) {
        for (const location of issue.occurrences.flatMap((occurrence) => occurrence.locations)) {
          allFilePaths.add(location.file);
        }
      }

      for (const tp of loadedSnapshotDetail.true_positives) {
        for (const occurrence of tp.occurrences) {
          for (const location of occurrence.locations) {
            allFilePaths.add(location.file);
          }
        }
      }
      for (const fp of loadedSnapshotDetail.false_positives) {
        for (const occurrence of fp.occurrences) {
          for (const location of occurrence.locations) {
            allFilePaths.add(location.file);
          }
        }
      }

      const newContents = new Map<string, FileContentResponse>();
      if (allFilePaths.size === 0) {
        setFileContents(newContents);
        return;
      }

      const response = await fetchSnapshotFiles(snapshotSlug, Array.from(allFilePaths));
      for (const content of response.files) {
        newContents.set(content.path, content);
      }
      for (const missingPath of response.missing) {
        console.error(`Failed to fetch file ${missingPath}: file missing from snapshot`);
      }
      setFileContents(newContents);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load snapshot data";
      toast.error(message);
    } finally {
      loadingSnapshotRef.current = false;
      setLoadingSnapshot(false);
    }
  }, []);

  const loadSnapshotDataInBackground = useCallback(
    (criticRun: AgentRunDetail) => {
      const reportedIssues = getReportedIssues(criticRun);
      if (
        getAgentType(criticRun) === "critic" &&
        reportedIssues.length > 0 &&
        !snapshotDetailRef.current &&
        !loadingSnapshotRef.current
      ) {
        void loadSnapshotData(criticRun);
      }
    },
    [loadSnapshotData]
  );

  const loadData = useCallback(async () => {
    setLoad({ status: "loading" });
    try {
      const fetchedRun = await fetchRun(runId);
      setLoad({ status: "loaded", run: fetchedRun });
      // Snapshot and file hydration stays in the background so metadata, logs, and LLM requests appear first.
      loadSnapshotDataInBackground(fetchedRun);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load run";
      setLoad({ status: "error", message });
      toast.error(message);
    }
  }, [runId, loadSnapshotDataInBackground]);

  const loadLLMRequests = useCallback(async () => {
    if (loadingLLMRequestsRef.current) return;
    loadingLLMRequestsRef.current = true;
    setLoadingLLMRequests(true);
    try {
      const response = await fetchLLMRequests(runId);
      setLlmRequests(response.requests);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load LLM requests";
      toast.error(message);
    } finally {
      loadingLLMRequestsRef.current = false;
      setLoadingLLMRequests(false);
    }
  }, [runId]);

  const loadLogs = useCallback(async () => {
    if (loadingLogsRef.current) return;
    loadingLogsRef.current = true;
    setLoadingLogs(true);
    try {
      const response = await fetchRunLogs(runId);
      setContainerLogs(response.logs);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load logs";
      toast.error(message);
    } finally {
      loadingLogsRef.current = false;
      setLoadingLogs(false);
    }
  }, [runId]);

  useEffect(() => {
    // Seeded visual tests skip all requests; otherwise load run metadata and LLM requests independently.
    if (initialRun) return;

    void loadData();
    void loadLLMRequests();
  }, [initialRun, loadData, loadLLMRequests]);

  return (
    <div className="bg-white dark:bg-gray-900 rounded-lg shadow dark:shadow-gray-950/30">
      <div className="p-4 border-b border-gray-200 dark:border-gray-700">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-4">
            <BackButton className="px-3 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" />
            <h2 className="text-lg font-semibold">Run Details</h2>
            {load.status === "loaded" ? (
              <span className="font-mono text-sm text-gray-500 dark:text-gray-400">
                <RunIdLink id={load.run.agent_run_id} />
              </span>
            ) : null}
          </div>
          {load.status === "loaded" ? (
            <span className={`px-2 py-1 rounded text-sm font-medium capitalize ${getStatusColor(load.run.status)}`}>
              {formatStatus(load.run.status)}
            </span>
          ) : null}
        </div>
        <Breadcrumb items={[{ label: "Home", href: "/" }, { label: "Runs", href: "/runs" }, { label: runId }]} />
      </div>

      {load.status === "loading" ? (
        <div className="p-4">
          <p className="text-gray-500 dark:text-gray-400">Loading...</p>
        </div>
      ) : load.status === "error" ? (
        <div className="p-4">
          <p className="text-red-500 dark:text-red-400">{load.message}</p>
        </div>
      ) : (
        <LoadedRunContent
          run={load.run}
          snapshotDetail={snapshotDetail}
          fileContents={fileContents}
          loadingSnapshot={loadingSnapshot}
          llmRequests={llmRequests}
          loadingLLMRequests={loadingLLMRequests}
          containerLogs={containerLogs}
          loadingLogs={loadingLogs}
          activeLogTab={activeLogTab}
          setActiveLogTab={setActiveLogTab}
          loadLLMRequests={loadLLMRequests}
          loadLogs={loadLogs}
        />
      )}
    </div>
  );
}

interface LoadedRunContentProps {
  run: AgentRunDetail;
  snapshotDetail: SnapshotDetailResponse | null;
  fileContents: Map<string, FileContentResponse>;
  loadingSnapshot: boolean;
  llmRequests: LLMRequestInfo[];
  loadingLLMRequests: boolean;
  containerLogs: string | null;
  loadingLogs: boolean;
  activeLogTab: LogTab;
  setActiveLogTab: (tab: LogTab) => void;
  loadLLMRequests: () => Promise<void>;
  loadLogs: () => Promise<void>;
}

function LoadedRunContent({
  run,
  snapshotDetail,
  fileContents,
  loadingSnapshot,
  llmRequests,
  loadingLLMRequests,
  containerLogs,
  loadingLogs,
  activeLogTab,
  setActiveLogTab,
  loadLLMRequests,
  loadLogs,
}: LoadedRunContentProps) {
  const agentType = getAgentType(run);
  const reportedIssues = getReportedIssues(run);
  const aggregateEdges = getAggregatedEdges(run);
  const gradingSummary = computeGradingSummary(run);

  return (
    <>
      <div className="p-4 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 flex-shrink-0">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div>
            <span className="text-gray-500 dark:text-gray-400">Type:</span>
            <span className="ml-1 capitalize">{agentType}</span>
          </div>
          <div>
            <span className="text-gray-500 dark:text-gray-400">Definition:</span>
            <span className="ml-1">
              <DefinitionIdLink id={run.image_digest} />
            </span>
          </div>
          <div>
            <span className="text-gray-500 dark:text-gray-400">Model:</span>
            <span className="ml-1">{run.model}</span>
          </div>
          <div>
            <span className="text-gray-500 dark:text-gray-400">LLM Calls:</span>
            <span className="ml-1">{run.llm_call_count}</span>
          </div>
          <div>
            <span className="text-gray-500 dark:text-gray-400">Budget:</span>
            <span className="ml-1">${run.budget_usd.toFixed(2)}</span>
          </div>
          {run.parent_agent_run_id ? (
            <div>
              <span className="text-gray-500 dark:text-gray-400">Parent:</span>
              <span className="ml-1">
                <RunIdLink id={run.parent_agent_run_id} />
              </span>
            </div>
          ) : null}
        </div>
      </div>

      <div className="px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 flex-shrink-0 text-sm">
        {agentType === "critic" ? (
          (() => {
            const config = run.type_config as CriticTypeConfig;
            const resolvedFiles = getResolvedFiles(run);
            return (
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                <span>
                  <span className="text-gray-500 dark:text-gray-400">Example:</span>
                  <ExampleLink example={config.example} />
                </span>
                {config.example.kind === "file_set" && resolvedFiles ? (
                  <span>
                    <span className="text-gray-500 dark:text-gray-400">Files:</span> {resolvedFiles.join(", ")}
                  </span>
                ) : null}
              </div>
            );
          })()
        ) : agentType === "grader" ? (
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            <span className="text-gray-500 dark:text-gray-400">Snapshot:</span>
            {(run.type_config as GraderTypeConfig).snapshot_slug}
          </div>
        ) : agentType === "critic_dev_improve" ? (
          (() => {
            const config = run.type_config as CriticDevImproveTypeConfig;
            return (
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                <span>
                  <span className="text-gray-500 dark:text-gray-400">Baselines:</span>
                  {config.baseline_image_digests.map((definitionId, index) => (
                    <Fragment key={definitionId}>
                      {index > 0 ? ", " : null}
                      <DefinitionIdLink id={definitionId} />
                    </Fragment>
                  ))}
                </span>
                <span>
                  <span className="text-gray-500 dark:text-gray-400">Examples:</span> {config.allowed_examples.length}
                </span>
                <span>
                  <span className="text-gray-500 dark:text-gray-400">Models:</span> improvement=
                  {config.improvement_model}, critic={config.critic_model}
                </span>
              </div>
            );
          })()
        ) : agentType === "critic_dev_optimize" ? (
          (() => {
            const config = run.type_config as CriticDevOptimizeTypeConfig;
            return (
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                <span>
                  <span className="text-gray-500 dark:text-gray-400">Target:</span> {config.target_metric}
                </span>
                <span>
                  <span className="text-gray-500 dark:text-gray-400">Budget:</span> ${run.budget_usd}
                </span>
                <span>
                  <span className="text-gray-500 dark:text-gray-400">Models:</span> optimizer={config.optimizer_model},
                  critic={config.critic_model}
                </span>
              </div>
            );
          })()
        ) : (
          <span className="text-gray-400 dark:text-gray-500 italic">No type-specific inputs</span>
        )}
      </div>

      {run.child_runs && run.child_runs.length > 0 ? (
        <div className="px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 flex-shrink-0 text-sm">
          <span className="text-gray-500 dark:text-gray-400">Child runs:</span>
          <span className="ml-2 flex flex-wrap gap-2">
            {run.child_runs.map((child) => (
              <span className="inline-flex items-center gap-1" key={child.agent_run_id}>
                <RunIdLink id={child.agent_run_id} />
                <span className="text-xs text-gray-400 dark:text-gray-500">({child.agent_type})</span>
              </span>
            ))}
          </span>
        </div>
      ) : null}

      {agentType === "critic" && gradingSummary ? (
        <div className="px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-blue-50 dark:bg-blue-950 flex-shrink-0 text-sm">
          <div className="flex flex-wrap gap-x-6 gap-y-1">
            <span>
              <span className="text-gray-500 dark:text-gray-400">Credit:</span>
              <span className="ml-1 font-medium">{gradingSummary.total_credit.toFixed(1)}</span>
            </span>
            <span className="text-green-600 dark:text-green-400" title="True Positives matched">
              Matched: {gradingSummary.tp_count} TPs
            </span>
            <span className="text-red-600 dark:text-red-400" title="False Positives hit">
              {gradingSummary.fp_count} FPs
            </span>
          </div>
        </div>
      ) : null}

      {agentType === "critic" && aggregateEdges.length > 0 ? (
        <div className="px-4 py-2 border-b border-gray-200 dark:border-gray-700 flex-shrink-0">
          <GradingEdges
            edges={aggregateEdges}
            missedOccurrences={[]}
            totalCredit={gradingSummary?.total_credit}
            recallDenominator={undefined}
            defaultOpen={aggregateEdges.filter((edge) => edge.target.credit > 0).length < 10}
            runId={run.agent_run_id}
            snapshotSlug={getSnapshotSlug(run)}
          />
        </div>
      ) : agentType === "grader" ? (
        (() => {
          const gradingEdges = getGradingEdges(run);
          if (gradingEdges.length === 0) return null;
          return (
            <div className="px-4 py-2 border-b border-gray-200 dark:border-gray-700 flex-shrink-0">
              <GradingEdges
                edges={gradingEdges}
                missedOccurrences={[]}
                defaultOpen={gradingEdges.filter((edge) => edge.target.credit > 0).length < 10}
                runId={run.agent_run_id}
                snapshotSlug={getSnapshotSlug(run)}
              />
            </div>
          );
        })()
      ) : null}

      {agentType === "critic" && reportedIssues.length > 0 && (snapshotDetail || loadingSnapshot) ? (
        <div className="border-b border-gray-200 dark:border-gray-700">
          <div className="px-4 py-3 bg-gray-100 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-700">
            <h3 className="text-md font-medium">Critique vs Ground Truth</h3>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
              Showing files with critique issues or ground truth annotations
            </p>
          </div>
          {loadingSnapshot ? (
            <div className="p-4">
              <p className="text-gray-500 dark:text-gray-400 text-sm">Loading snapshot data...</p>
            </div>
          ) : snapshotDetail ? (
            <div className="p-4 space-y-6">
              {Array.from(fileContents.entries()).map(([filePath, fileContent]) => (
                <FileViewer
                  key={filePath}
                  file={fileContent}
                  tps={snapshotDetail.true_positives}
                  fps={snapshotDetail.false_positives}
                  critiqueIssues={reportedIssues}
                  gradingEdges={aggregateEdges}
                  snapshotSlug={getSnapshotSlug(run)}
                  defaultCollapsed={true}
                />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="border-t border-gray-200 dark:border-gray-700">
        <div className="px-4 py-3 bg-gray-100 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-700 flex items-center gap-4">
          <h3 className="text-md font-medium">Logs &amp; LLM Requests</h3>
          <div className="flex gap-1">
            <Button
              unstyled
              className={`px-3 py-1 text-sm rounded ${
                activeLogTab === "llm"
                  ? "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
                  : "bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-300 dark:hover:bg-gray-500"
              }`}
              type="button"
              onClick={() => {
                setActiveLogTab("llm");
                void loadLLMRequests();
              }}
            >
              LLM Requests ({run.llm_call_count})
            </Button>
            <Button
              unstyled
              className={`px-3 py-1 text-sm rounded ${
                activeLogTab === "logs"
                  ? "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
                  : "bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-300 dark:hover:bg-gray-500"
              }`}
              type="button"
              onClick={() => {
                setActiveLogTab("logs");
                void loadLogs();
              }}
            >
              Logs
            </Button>
          </div>
        </div>

        {activeLogTab === "logs" ? (
          <div className="p-4">
            {loadingLogs ? (
              <p className="text-gray-500 dark:text-gray-400">Loading logs...</p>
            ) : containerLogs ? (
              <pre className="bg-gray-900 text-gray-100 p-4 rounded text-sm overflow-auto max-h-96 whitespace-pre-wrap">
                {containerLogs}
              </pre>
            ) : (
              <p className="text-gray-500 dark:text-gray-400 italic">No logs</p>
            )}
          </div>
        ) : (
          <div className="p-4">
            {run.llm_costs ? (
              <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm mb-4 pb-4 border-b border-gray-200 dark:border-gray-700">
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Requests:</span>
                  <span className="ml-1 font-medium">{run.llm_costs.totals.requests.toLocaleString()}</span>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Input:</span>
                  <span className="ml-1 font-medium">{run.llm_costs.totals.input_tokens.toLocaleString()}</span>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Cached:</span>
                  <span className="ml-1 font-medium">{run.llm_costs.totals.cached_tokens.toLocaleString()}</span>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Output:</span>
                  <span className="ml-1 font-medium">{run.llm_costs.totals.output_tokens.toLocaleString()}</span>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Cost:</span>
                  <span className="ml-1 font-medium text-green-600 dark:text-green-400">
                    ${run.llm_costs.totals.cost_usd.toFixed(4)}
                  </span>
                </div>
              </div>
            ) : null}
            {loadingLLMRequests ? (
              <p className="text-gray-500 dark:text-gray-400">Loading LLM requests...</p>
            ) : (
              <LLMRequestViewer requests={llmRequests} />
            )}
          </div>
        )}
      </div>
    </>
  );
}
