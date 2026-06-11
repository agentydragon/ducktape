<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { toast } from "svelte-sonner";
  import SvelteMarkdown from "@humanspeak/svelte-markdown";
  import { SvelteMap } from "svelte/reactivity";
  import BackButton from "./BackButton.svelte";
  import Breadcrumb from "./Breadcrumb.svelte";
  import LLMRequestViewer from "./LLMRequestViewer.svelte";
  import {
    fetchRun,
    fetchSnapshotDetail,
    fetchSnapshotFile,
    fetchLLMRequests,
    type AgentRunDetail,
    type CriticTypeConfig,
    type GraderTypeConfig,
    type ImprovementTypeConfig,
    type PromptOptimizerTypeConfig,
    type SnapshotDetailResponse,
    type FileContentResponse,
    type GradingEdgeInfo,
    type ReportedIssueInfo,
    type LLMRequestInfo,
    isCriticRun,
    isGraderRun,
  } from "../lib/api/client";
  import { getStatusColor, formatStatus } from "../lib/status";
  import RunIdLink from "../lib/RunIdLink.svelte";
  import DefinitionIdLink from "../lib/DefinitionIdLink.svelte";
  import ExampleLink from "../lib/ExampleLink.svelte";
  import GradingEdges from "./GradingEdges.svelte";
  import FileViewer from "./FileViewer.svelte";

  // Props
  interface Props {
    runId: string;
  }
  let { runId }: Props = $props();

  // State
  let run: AgentRunDetail | null = $state(null);
  let loading = $state(true);
  let pollInterval: ReturnType<typeof setInterval> | null = null;

  // Critique viewer state
  let snapshotDetail: SnapshotDetailResponse | null = $state(null);
  let fileContents = $state(new SvelteMap<string, FileContentResponse>());
  let loadingSnapshot = $state(false);

  // LLM requests state
  let llmRequests: LLMRequestInfo[] = $state([]);
  let loadingLLMRequests = $state(false);

  // Tab state for logs/LLM view
  type LogTab = "stdout" | "stderr" | "llm";
  let activeLogTab: LogTab = $state("llm");

  // --- Helpers ---

  // Get agent type from discriminator field
  function getAgentType(run: AgentRunDetail): string {
    return "agent_type" in run.details ? run.details.agent_type : "unknown";
  }

  // Get reported issues from critic run details
  function getReportedIssues(run: AgentRunDetail): ReportedIssueInfo[] {
    if (!isCriticRun(run)) return [];
    return run.details.reported_issues;
  }

  // Get grading edges from grader run details
  function getGradingEdges(run: AgentRunDetail): GradingEdgeInfo[] {
    if (!isGraderRun(run)) return [];
    return run.details.grading_edges;
  }

  // Get resolved files from critic run details
  function getResolvedFiles(run: AgentRunDetail): string[] | null {
    if (!isCriticRun(run)) return null;
    return run.details.resolved_files;
  }

  // Get snapshot slug from run's example (for critics and graders)
  function getSnapshotSlug(run: AgentRunDetail): string | undefined {
    const config = run.type_config;
    if ("example" in config && config.example) {
      return config.example.snapshot_slug;
    }
    return undefined;
  }

  // Compute aggregated grading edges from all grader runs
  function getAggregatedEdges(run: AgentRunDetail): GradingEdgeInfo[] {
    if (!isCriticRun(run)) return [];
    return run.details.grader_runs.flatMap((g) => g.grading_edges);
  }

  // Compute grading summary from aggregated edges
  function computeGradingSummary(run: AgentRunDetail) {
    if (!isCriticRun(run)) return null;

    const edges = getAggregatedEdges(run);
    if (edges.length === 0) return null;

    const tp_count = edges.filter((e) => e.target.kind === "tp").length;
    const fp_count = edges.filter((e) => e.target.kind === "fp").length;
    const total_credit = edges.filter((e) => e.target.kind === "tp").reduce((sum, e) => sum + e.target.credit, 0);

    // Recall denominator needs to come from example - we'll pass it separately
    return { tp_count, fp_count, total_credit };
  }

  // Load run data
  async function loadData() {
    try {
      run = await fetchRun(runId);

      // Load snapshot data for critic runs with reported issues
      const reportedIssues = getReportedIssues(run);
      if (getAgentType(run) === "critic" && reportedIssues.length > 0) {
        await loadSnapshotData(run);
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : "Failed to load run";
      toast.error(message);
    } finally {
      loading = false;
    }
  }

  // Load LLM requests
  async function loadLLMRequests() {
    if (loadingLLMRequests || llmRequests.length > 0) return;
    loadingLLMRequests = true;
    try {
      const response = await fetchLLMRequests(runId);
      llmRequests = response.requests;
    } catch (e) {
      const message = e instanceof Error ? e.message : "Failed to load LLM requests";
      toast.error(message);
    } finally {
      loadingLLMRequests = false;
    }
  }

  // Load snapshot and file data for critique viewer
  async function loadSnapshotData(criticRun: AgentRunDetail) {
    if (getAgentType(criticRun) !== "critic") return;

    const config = criticRun.type_config as CriticTypeConfig;
    let snapshotSlug: string;

    // Extract snapshot slug from example
    if (config.example.kind === "whole_snapshot") {
      snapshotSlug = config.example.snapshot_slug;
    } else {
      snapshotSlug = config.example.snapshot_slug;
    }

    loadingSnapshot = true;
    try {
      // Fetch snapshot detail to get ground truth
      snapshotDetail = await fetchSnapshotDetail(snapshotSlug);

      // Collect all files mentioned in critique issues or ground truth
      const allFilePaths = new SvelteSet<string>();

      // Files from critique issues
      const reportedIssues = getReportedIssues(criticRun);
      for (const issue of reportedIssues) {
        for (const fileLocation of issue.occurrences.flatMap((o) => o.files)) {
          allFilePaths.add(fileLocation.path);
        }
      }

      // Files from ground truth
      for (const tp of snapshotDetail.true_positives) {
        for (const occ of tp.occurrences) {
          for (const fileInfo of occ.files) {
            allFilePaths.add(fileInfo.path);
          }
        }
      }
      for (const fp of snapshotDetail.false_positives) {
        for (const occ of fp.occurrences) {
          for (const fileInfo of occ.files) {
            allFilePaths.add(fileInfo.path);
          }
        }
      }

      // Fetch file contents
      const newContents = new SvelteMap<string, FileContentResponse>();
      await Promise.all(
        Array.from(allFilePaths).map(async (path) => {
          try {
            const content = await fetchSnapshotFile(snapshotSlug, path);
            newContents.set(path, content);
          } catch (e) {
            console.error(`Failed to fetch file ${path}:`, e);
          }
        })
      );
      fileContents = newContents;
    } catch (e) {
      const message = e instanceof Error ? e.message : "Failed to load snapshot data";
      toast.error(message);
    } finally {
      loadingSnapshot = false;
    }
  }

  onMount(() => {
    loadData().then(() => {
      // Load LLM requests after run data is loaded (LLM tab is default)
      if (run) {
        loadLLMRequests();
      }
    });
    // Poll while in progress
    pollInterval = setInterval(() => {
      if (run?.status === "in_progress") {
        loadData();
      }
    }, 1000);
  });

  onDestroy(() => {
    if (pollInterval) clearInterval(pollInterval);
  });
</script>

<div class="bg-white rounded-lg shadow">
  <!-- Header -->
  <div class="p-4 border-b">
    <div class="flex items-center justify-between mb-3">
      <div class="flex items-center gap-4">
        <BackButton class="px-3 py-1 text-sm border border-gray-300 rounded bg-white text-gray-700 hover:bg-gray-50" />
        <h2 class="text-lg font-semibold">Run Details</h2>
        {#if run}
          <span class="font-mono text-sm text-gray-500"><RunIdLink id={run.agent_run_id} /></span>
        {/if}
      </div>
      {#if run}
        <span class="px-2 py-1 rounded text-sm font-medium capitalize {getStatusColor(run.status)}">
          {formatStatus(run.status)}
        </span>
      {/if}
    </div>
    <Breadcrumb items={[{ label: "Home", href: "/" }, { label: "Runs", href: "/runs" }, { label: runId }]} />
  </div>

  {#if loading}
    <div class="p-4">
      <p class="text-gray-500">Loading...</p>
    </div>
  {:else if run}
    <!-- Run info -->
    <div class="p-4 border-b bg-gray-50 flex-shrink-0">
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <div>
          <span class="text-gray-500">Type:</span>
          <span class="ml-1 capitalize">{getAgentType(run)}</span>
        </div>
        <div>
          <span class="text-gray-500">Definition:</span>
          <span class="ml-1"><DefinitionIdLink id={run.image_digest} /></span>
        </div>
        <div>
          <span class="text-gray-500">Model:</span>
          <span class="ml-1">{run.model}</span>
        </div>
        <div>
          <span class="text-gray-500">LLM Calls:</span>
          <span class="ml-1">{run.llm_call_count}</span>
        </div>
        {#if run.parent_agent_run_id}
          <div>
            <span class="text-gray-500">Parent:</span>
            <span class="ml-1"><RunIdLink id={run.parent_agent_run_id} /></span>
          </div>
        {/if}
      </div>
    </div>

    <!-- Type-specific inputs -->
    <div class="px-4 py-2 border-b bg-gray-50 flex-shrink-0 text-sm">
      {#if getAgentType(run) === "critic"}}
        {@const config = run.type_config as CriticTypeConfig}
        {@const resolvedFiles = getResolvedFiles(run)}
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          <span>
            <span class="text-gray-500">Example:</span>
            <ExampleLink example={config.example} />
          </span>
          {#if config.example.kind === "file_set" && resolvedFiles}
            <span><span class="text-gray-500">Files:</span> {resolvedFiles.join(", ")}</span>
          {/if}
        </div>
      {:else if getAgentType(run) === "grader"}
        {@const config = run.type_config as GraderTypeConfig}
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          <span class="text-gray-500">Grading critic:</span>
          <RunIdLink id={config.graded_agent_run_id} />
        </div>
      {:else if getAgentType(run) === "improvement"}
        {@const config = run.type_config as ImprovementTypeConfig}
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          <span
            ><span class="text-gray-500">Baselines:</span>
            {#each config.baseline_image_refs as defId, i (defId)}
              {#if i > 0},
              {/if}<DefinitionIdLink id={defId} />
            {/each}
          </span>
          <span><span class="text-gray-500">Examples:</span> {config.allowed_examples.length}</span>
          <span
            ><span class="text-gray-500">Models:</span> improvement={config.improvement_model}, critic={config.critic_model},
            grader={config.grader_model}</span
          >
        </div>
      {:else if getAgentType(run) === "prompt_optimizer"}
        {@const config = run.type_config as PromptOptimizerTypeConfig}
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          <span><span class="text-gray-500">Target:</span> {config.target_metric}</span>
          <span><span class="text-gray-500">Budget:</span> ${config.budget_limit}</span>
          <span
            ><span class="text-gray-500">Models:</span> optimizer={config.optimizer_model}, critic={config.critic_model},
            grader={config.grader_model}</span
          >
        </div>
      {:else}
        <span class="text-gray-400 italic">No type-specific inputs</span>
      {/if}
    </div>

    <!-- Child runs (for critic runs: show linked grader runs) -->
    {#if run.child_runs && run.child_runs.length > 0}
      <div class="px-4 py-2 border-b bg-gray-50 flex-shrink-0 text-sm">
        <span class="text-gray-500">Child runs:</span>
        <span class="ml-2 flex flex-wrap gap-2">
          {#each run.child_runs as child (child.agent_run_id)}
            <span class="inline-flex items-center gap-1">
              <RunIdLink id={child.agent_run_id} />
              <span class="text-xs text-gray-400">({child.agent_type})</span>
            </span>
          {/each}
        </span>
      </div>
    {/if}

    <!-- Grading summary (for critic runs with completed grader) -->
    {#if getAgentType(run) === "critic"}
      {@const gs = computeGradingSummary(run)}
      {#if gs}
        {@const recall_denominator = 0}
        {@const recall = recall_denominator > 0 ? gs.total_credit / recall_denominator : null}
        {@const recallColor =
          recall == null
            ? "text-gray-400"
            : recall >= 0.7
              ? "text-green-600"
              : recall >= 0.4
                ? "text-yellow-600"
                : "text-red-600"}
        <div class="px-4 py-2 border-b bg-blue-50 flex-shrink-0 text-sm">
          <div class="flex flex-wrap gap-x-6 gap-y-1">
            <span>
              <span class="text-gray-500">Credit:</span>
              <span class="ml-1 font-medium {recallColor}">
                {gs.total_credit.toFixed(1)}{#if recall_denominator > 0}
                  / {recall_denominator} expected{/if}
              </span>
              {#if recall != null}
                <span class="text-gray-400 text-xs">({(recall * 100).toFixed(0)}%)</span>
              {/if}
            </span>
            <span class="text-green-600" title="True Positives matched">Matched: {gs.tp_count} TPs</span>
            <span class="text-red-600" title="False Positives hit">{gs.fp_count} FPs</span>
          </div>
        </div>
      {/if}
    {/if}

    <!-- Grading edges (for both critic and grader runs) -->
    {#if getAgentType(run) === "critic"}
      {@const edges = getAggregatedEdges(run)}
      {#if edges.length > 0}
        {@const visibleEdges = edges.filter((e) => e.target.credit > 0)}
        {@const gs = computeGradingSummary(run)}
        <div class="px-4 py-2 border-b flex-shrink-0">
          <GradingEdges
            {edges}
            missedOccurrences={[]}
            totalCredit={gs?.total_credit}
            recallDenominator={undefined}
            defaultOpen={visibleEdges.length < 10}
            runId={run.agent_run_id}
            snapshotSlug={getSnapshotSlug(run)}
          />
        </div>
      {/if}
    {:else if getAgentType(run) === "grader"}
      {@const gradingEdges = getGradingEdges(run)}
      {#if gradingEdges.length > 0}
        {@const visibleEdges = gradingEdges.filter((e) => e.target.credit > 0)}
        <div class="px-4 py-2 border-b flex-shrink-0">
          <GradingEdges
            edges={gradingEdges}
            missedOccurrences={[]}
            defaultOpen={visibleEdges.length < 10}
            runId={run.agent_run_id}
            snapshotSlug={getSnapshotSlug(run)}
          />
        </div>
      {/if}
    {/if}

    <!-- Critique file viewer (for critic runs with reported issues) -->
    {@const reportedIssues = getReportedIssues(run)}
    {#if getAgentType(run) === "critic" && reportedIssues.length > 0 && snapshotDetail}
      {@const edges = getAggregatedEdges(run)}
      <div class="border-b">
        <div class="px-4 py-3 bg-gray-100 border-b">
          <h3 class="text-md font-medium">Critique vs Ground Truth</h3>
          <p class="text-sm text-gray-600 mt-1">Showing files with critique issues or ground truth annotations</p>
        </div>
        {#if loadingSnapshot}
          <div class="p-4">
            <p class="text-gray-500 text-sm">Loading snapshot data...</p>
          </div>
        {:else}
          <div class="p-4 space-y-6">
            {#each Array.from(fileContents.entries()) as [filePath, fileContent] (filePath)}
              <FileViewer
                file={fileContent}
                tps={snapshotDetail.true_positives}
                fps={snapshotDetail.false_positives}
                critiqueIssues={reportedIssues}
                gradingEdges={edges}
                snapshotSlug={getSnapshotSlug(run)}
              />
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <!-- Logs and LLM Requests Section -->
    <div class="border-t">
      <div class="px-4 py-3 bg-gray-100 border-b flex items-center gap-4">
        <h3 class="text-md font-medium">Logs & LLM Requests</h3>
        <div class="flex gap-1">
          <button
            class="px-3 py-1 text-sm rounded {activeLogTab === 'llm'
              ? 'bg-blue-100 text-blue-700'
              : 'bg-gray-200 text-gray-700 hover:bg-gray-300'}"
            onclick={() => {
              activeLogTab = "llm";
              loadLLMRequests();
            }}
          >
            LLM Requests ({run.llm_call_count})
          </button>
          <button
            class="px-3 py-1 text-sm rounded {activeLogTab === 'stdout'
              ? 'bg-blue-100 text-blue-700'
              : 'bg-gray-200 text-gray-700 hover:bg-gray-300'}"
            onclick={() => (activeLogTab = "stdout")}
          >
            stdout
          </button>
          <button
            class="px-3 py-1 text-sm rounded {activeLogTab === 'stderr'
              ? 'bg-blue-100 text-blue-700'
              : 'bg-gray-200 text-gray-700 hover:bg-gray-300'}"
            onclick={() => (activeLogTab = "stderr")}
          >
            stderr
          </button>
        </div>
      </div>

      {#if activeLogTab === "stdout"}
        <div class="p-4">
          {#if run.container_stdout}
            <pre
              class="bg-gray-900 text-gray-100 p-4 rounded text-sm overflow-auto max-h-96 whitespace-pre-wrap">{run.container_stdout}</pre>
          {:else}
            <p class="text-gray-500 italic">No stdout captured</p>
          {/if}
        </div>
      {:else if activeLogTab === "stderr"}
        <div class="p-4">
          {#if run.container_stderr}
            <pre
              class="bg-gray-900 text-gray-100 p-4 rounded text-sm overflow-auto max-h-96 whitespace-pre-wrap">{run.container_stderr}</pre>
          {:else}
            <p class="text-gray-500 italic">No stderr captured</p>
          {/if}
        </div>
      {:else if activeLogTab === "llm"}
        <div class="p-4">
          {#if run.llm_costs}
            <div class="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm mb-4 pb-4 border-b">
              <div>
                <span class="text-gray-500">Requests:</span>
                <span class="ml-1 font-medium">{run.llm_costs.total_requests.toLocaleString()}</span>
              </div>
              <div>
                <span class="text-gray-500">Input:</span>
                <span class="ml-1 font-medium">{run.llm_costs.total_input_tokens.toLocaleString()}</span>
              </div>
              <div>
                <span class="text-gray-500">Cached:</span>
                <span class="ml-1 font-medium">{run.llm_costs.total_cached_tokens.toLocaleString()}</span>
              </div>
              <div>
                <span class="text-gray-500">Output:</span>
                <span class="ml-1 font-medium">{run.llm_costs.total_output_tokens.toLocaleString()}</span>
              </div>
              <div>
                <span class="text-gray-500">Cost:</span>
                <span class="ml-1 font-medium text-green-600">${run.llm_costs.total_cost_usd.toFixed(4)}</span>
              </div>
            </div>
          {/if}
          {#if loadingLLMRequests}
            <p class="text-gray-500">Loading LLM requests...</p>
          {:else}
            <LLMRequestViewer requests={llmRequests} />
          {/if}
        </div>
      {/if}
    </div>
  {:else}
    <div class="p-4">
      <p class="text-red-500">Failed to load run</p>
    </div>
  {/if}
</div>
