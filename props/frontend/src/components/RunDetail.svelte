<script lang="ts">
  import { onMount } from "svelte";
  import { toast } from "svelte-sonner";
  import { SvelteMap, SvelteSet } from "svelte/reactivity";
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
  import RunIdLink from "../lib/RunIdLink.svelte";
  import DefinitionIdLink from "../lib/DefinitionIdLink.svelte";
  import ExampleLink from "../lib/ExampleLink.svelte";
  import GradingEdges from "./GradingEdges.svelte";
  import FileViewer from "./FileViewer.svelte";

  // Props
  interface Props {
    runId: string;
    initialRun?: AgentRunDetail;
    initialSnapshotDetail?: SnapshotDetailResponse;
    initialFileContents?: Map<string, FileContentResponse>;
    initialLLMRequests?: LLMRequestInfo[];
  }
  let { runId, initialRun, initialSnapshotDetail, initialFileContents, initialLLMRequests }: Props = $props();

  // State
  let run: AgentRunDetail | null = $state(initialRun ?? null);
  let loading = $state(!initialRun);

  // Critique viewer state
  let snapshotDetail: SnapshotDetailResponse | null = $state(initialSnapshotDetail ?? null);
  let fileContents = $state(
    initialFileContents ? new SvelteMap(initialFileContents) : new SvelteMap<string, FileContentResponse>()
  );
  let loadingSnapshot = $state(false);

  // LLM requests state
  let llmRequests: LLMRequestInfo[] = $state(initialLLMRequests ?? []);
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

    const tp_count = edges.filter((e) => e.target.kind === "tp" && e.target.credit > 0).length;
    const fp_count = edges.filter((e) => e.target.kind === "fp" && e.target.credit > 0).length;
    const total_credit = edges.filter((e) => e.target.kind === "tp").reduce((sum, e) => sum + e.target.credit, 0);

    return { tp_count, fp_count, total_credit };
  }

  // Load run data. snapshotLoaded=true skips re-fetching snapshot+files during polling.
  async function loadData(snapshotLoaded = false) {
    try {
      run = await fetchRun(runId);

      // Load snapshot data for critic runs with reported issues (only on initial load)
      if (!snapshotLoaded) {
        const reportedIssues = getReportedIssues(run);
        if (getAgentType(run) === "critic" && reportedIssues.length > 0) {
          await loadSnapshotData(run);
        }
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : "Failed to load run";
      toast.error(message);
    } finally {
      loading = false;
    }
  }

  let llmRequestsFetched = false;

  // Load LLM requests
  async function loadLLMRequests() {
    if (loadingLLMRequests) return;
    loadingLLMRequests = true;
    try {
      const response = await fetchLLMRequests(runId);
      llmRequests = response.requests;
      llmRequestsFetched = true;
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
    const snapshotSlug = config.example.snapshot_slug;

    loadingSnapshot = true;
    try {
      // Fetch snapshot detail filtered to this example's recall scope
      const exampleKind = config.example.kind;
      const filesHash = exampleKind === "file_set" ? config.example.files_hash : undefined;
      snapshotDetail = await fetchSnapshotDetail(snapshotSlug, exampleKind, filesHash);

      // Collect all files mentioned in critique issues or ground truth
      const allFilePaths = new SvelteSet<string>();

      // Files from critique issues
      const reportedIssues = getReportedIssues(criticRun);
      for (const issue of reportedIssues) {
        for (const loc of issue.occurrences.flatMap((o) => o.locations)) {
          allFilePaths.add(loc.file);
        }
      }

      // Files from ground truth
      for (const tp of snapshotDetail.true_positives) {
        for (const occ of tp.occurrences) {
          for (const loc of occ.locations) {
            allFilePaths.add(loc.file);
          }
        }
      }
      for (const fp of snapshotDetail.false_positives) {
        for (const occ of fp.occurrences) {
          for (const loc of occ.locations) {
            allFilePaths.add(loc.file);
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
    // Skip fetching when initial data is provided (visual tests)
    if (initialRun) return;

    loadData().then(() => {
      // Load LLM requests after run data is loaded (LLM tab is default)
      if (run) {
        loadLLMRequests();
      }
    });
  });
</script>

<div class="bg-white dark:bg-gray-900 rounded-lg shadow dark:shadow-gray-950/30">
  <!-- Header -->
  <div class="p-4 border-b border-gray-200 dark:border-gray-700">
    <div class="flex items-center justify-between mb-3">
      <div class="flex items-center gap-4">
        <BackButton
          class="px-3 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
        />
        <h2 class="text-lg font-semibold">Run Details</h2>
        {#if run}
          <span class="font-mono text-sm text-gray-500 dark:text-gray-400"><RunIdLink id={run.agent_run_id} /></span>
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
      <p class="text-gray-500 dark:text-gray-400">Loading...</p>
    </div>
  {:else if run}
    <!-- Run info -->
    <div class="p-4 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 flex-shrink-0">
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <div>
          <span class="text-gray-500 dark:text-gray-400">Type:</span>
          <span class="ml-1 capitalize">{getAgentType(run)}</span>
        </div>
        <div>
          <span class="text-gray-500 dark:text-gray-400">Definition:</span>
          <span class="ml-1"><DefinitionIdLink id={run.image_digest} /></span>
        </div>
        <div>
          <span class="text-gray-500 dark:text-gray-400">Model:</span>
          <span class="ml-1">{run.model}</span>
        </div>
        <div>
          <span class="text-gray-500 dark:text-gray-400">LLM Calls:</span>
          <span class="ml-1">{run.llm_call_count}</span>
        </div>
        <div>
          <span class="text-gray-500 dark:text-gray-400">Budget:</span>
          <span class="ml-1">${run.budget_usd.toFixed(2)}</span>
        </div>
        {#if run.parent_agent_run_id}
          <div>
            <span class="text-gray-500 dark:text-gray-400">Parent:</span>
            <span class="ml-1"><RunIdLink id={run.parent_agent_run_id} /></span>
          </div>
        {/if}
      </div>
    </div>

    <!-- Type-specific inputs -->
    <div
      class="px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 flex-shrink-0 text-sm"
    >
      {#if getAgentType(run) === "critic"}
        {@const config = run.type_config as CriticTypeConfig}
        {@const resolvedFiles = getResolvedFiles(run)}
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          <span>
            <span class="text-gray-500 dark:text-gray-400">Example:</span>
            <ExampleLink example={config.example} />
          </span>
          {#if config.example.kind === "file_set" && resolvedFiles}
            <span><span class="text-gray-500 dark:text-gray-400">Files:</span> {resolvedFiles.join(", ")}</span>
          {/if}
        </div>
      {:else if getAgentType(run) === "grader"}
        {@const config = run.type_config as GraderTypeConfig}
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          <span class="text-gray-500 dark:text-gray-400">Snapshot:</span>
          {config.snapshot_slug}
        </div>
      {:else if getAgentType(run) === "critic_dev_improve"}
        {@const config = run.type_config as CriticDevImproveTypeConfig}
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          <span
            ><span class="text-gray-500 dark:text-gray-400">Baselines:</span>
            {#each config.baseline_image_digests as defId, i (defId)}
              {#if i > 0},
              {/if}<DefinitionIdLink id={defId} />
            {/each}
          </span>
          <span><span class="text-gray-500 dark:text-gray-400">Examples:</span> {config.allowed_examples.length}</span>
          <span
            ><span class="text-gray-500 dark:text-gray-400">Models:</span> improvement={config.improvement_model},
            critic={config.critic_model}</span
          >
        </div>
      {:else if getAgentType(run) === "critic_dev_optimize"}
        {@const config = run.type_config as CriticDevOptimizeTypeConfig}
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          <span><span class="text-gray-500 dark:text-gray-400">Target:</span> {config.target_metric}</span>
          <span><span class="text-gray-500 dark:text-gray-400">Budget:</span> ${run.budget_usd}</span>
          <span
            ><span class="text-gray-500 dark:text-gray-400">Models:</span> optimizer={config.optimizer_model}, critic={config.critic_model}</span
          >
        </div>
      {:else}
        <span class="text-gray-400 dark:text-gray-500 italic">No type-specific inputs</span>
      {/if}
    </div>

    <!-- Child runs (for critic runs: show linked grader runs) -->
    {#if run.child_runs && run.child_runs.length > 0}
      <div
        class="px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 flex-shrink-0 text-sm"
      >
        <span class="text-gray-500 dark:text-gray-400">Child runs:</span>
        <span class="ml-2 flex flex-wrap gap-2">
          {#each run.child_runs as child (child.agent_run_id)}
            <span class="inline-flex items-center gap-1">
              <RunIdLink id={child.agent_run_id} />
              <span class="text-xs text-gray-400 dark:text-gray-500">({child.agent_type})</span>
            </span>
          {/each}
        </span>
      </div>
    {/if}

    <!-- Grading summary (for critic runs with completed grader) -->
    {#if getAgentType(run) === "critic"}
      {@const gs = computeGradingSummary(run)}
      {#if gs}
        <div
          class="px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-blue-50 dark:bg-blue-950 flex-shrink-0 text-sm"
        >
          <div class="flex flex-wrap gap-x-6 gap-y-1">
            <span>
              <span class="text-gray-500 dark:text-gray-400">Credit:</span>
              <span class="ml-1 font-medium">{gs.total_credit.toFixed(1)}</span>
            </span>
            <span class="text-green-600 dark:text-green-400" title="True Positives matched"
              >Matched: {gs.tp_count} TPs</span
            >
            <span class="text-red-600 dark:text-red-400" title="False Positives hit">{gs.fp_count} FPs</span>
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
        <div class="px-4 py-2 border-b border-gray-200 dark:border-gray-700 flex-shrink-0">
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
        <div class="px-4 py-2 border-b border-gray-200 dark:border-gray-700 flex-shrink-0">
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
      <div class="border-b border-gray-200 dark:border-gray-700">
        <div class="px-4 py-3 bg-gray-100 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-700">
          <h3 class="text-md font-medium">Critique vs Ground Truth</h3>
          <p class="text-sm text-gray-600 dark:text-gray-400 mt-1">
            Showing files with critique issues or ground truth annotations
          </p>
        </div>
        {#if loadingSnapshot}
          <div class="p-4">
            <p class="text-gray-500 dark:text-gray-400 text-sm">Loading snapshot data...</p>
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
                defaultCollapsed={true}
              />
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <!-- Logs and LLM Requests Section -->
    <div class="border-t border-gray-200 dark:border-gray-700">
      <div
        class="px-4 py-3 bg-gray-100 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-700 flex items-center gap-4"
      >
        <h3 class="text-md font-medium">Logs & LLM Requests</h3>
        <div class="flex gap-1">
          <button
            class="px-3 py-1 text-sm rounded {activeLogTab === 'llm'
              ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300'
              : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-300 dark:hover:bg-gray-500'}"
            onclick={() => {
              activeLogTab = "llm";
              loadLLMRequests();
            }}
          >
            LLM Requests ({run.llm_call_count})
          </button>
          <button
            class="px-3 py-1 text-sm rounded {activeLogTab === 'stdout'
              ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300'
              : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-300 dark:hover:bg-gray-500'}"
            onclick={() => (activeLogTab = "stdout")}
          >
            stdout
          </button>
          <button
            class="px-3 py-1 text-sm rounded {activeLogTab === 'stderr'
              ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300'
              : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-300 dark:hover:bg-gray-500'}"
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
            <p class="text-gray-500 dark:text-gray-400 italic">No stdout captured</p>
          {/if}
        </div>
      {:else if activeLogTab === "stderr"}
        <div class="p-4">
          {#if run.container_stderr}
            <pre
              class="bg-gray-900 text-gray-100 p-4 rounded text-sm overflow-auto max-h-96 whitespace-pre-wrap">{run.container_stderr}</pre>
          {:else}
            <p class="text-gray-500 dark:text-gray-400 italic">No stderr captured</p>
          {/if}
        </div>
      {:else if activeLogTab === "llm"}
        <div class="p-4">
          {#if run.llm_costs}
            <div
              class="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm mb-4 pb-4 border-b border-gray-200 dark:border-gray-700"
            >
              <div>
                <span class="text-gray-500 dark:text-gray-400">Requests:</span>
                <span class="ml-1 font-medium">{run.llm_costs.totals.requests.toLocaleString()}</span>
              </div>
              <div>
                <span class="text-gray-500 dark:text-gray-400">Input:</span>
                <span class="ml-1 font-medium">{run.llm_costs.totals.input_tokens.toLocaleString()}</span>
              </div>
              <div>
                <span class="text-gray-500 dark:text-gray-400">Cached:</span>
                <span class="ml-1 font-medium">{run.llm_costs.totals.cached_tokens.toLocaleString()}</span>
              </div>
              <div>
                <span class="text-gray-500 dark:text-gray-400">Output:</span>
                <span class="ml-1 font-medium">{run.llm_costs.totals.output_tokens.toLocaleString()}</span>
              </div>
              <div>
                <span class="text-gray-500 dark:text-gray-400">Cost:</span>
                <span class="ml-1 font-medium text-green-600 dark:text-green-400"
                  >${run.llm_costs.totals.cost_usd.toFixed(4)}</span
                >
              </div>
            </div>
          {/if}
          {#if loadingLLMRequests}
            <p class="text-gray-500 dark:text-gray-400">Loading LLM requests...</p>
          {:else}
            <LLMRequestViewer requests={llmRequests} />
          {/if}
        </div>
      {/if}
    </div>
  {:else}
    <div class="p-4">
      <p class="text-red-500 dark:text-red-400">Failed to load run</p>
    </div>
  {/if}
</div>
