<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { toast } from "svelte-sonner";
  import SvelteMarkdown from "@humanspeak/svelte-markdown";
  import { SvelteMap, SvelteSet } from "svelte/reactivity";
  import BackButton from "./BackButton.svelte";
  import Breadcrumb from "./Breadcrumb.svelte";
  import {
    fetchRun,
    fetchRunEvents,
    fetchSnapshotDetail,
    fetchSnapshotFile,
    type AgentRunDetail,
    type EventInfo,
    type CriticTypeConfig,
    type GraderTypeConfig,
    type ImprovementTypeConfig,
    type PromptOptimizerTypeConfig,
    type ExecInput,
    type BaseExecResult,
    type TruncatedStream,
    type DockerExecCallPayload,
    type DockerExecOutputPayload,
    type SnapshotDetailResponse,
    type FileContentResponse,
    type GradingEdgeInfo,
    type ReportedIssueInfo,
    isCriticRun,
    isGraderRun,
  } from "../lib/api/client";
  import { getStatusColor, formatStatus } from "../lib/status";
  import { truncateText } from "../lib/formatters";
  import RunIdLink from "../lib/RunIdLink.svelte";
  import DefinitionIdLink from "../lib/DefinitionIdLink.svelte";
  import ExampleLink from "../lib/ExampleLink.svelte";
  import GradingEdges from "./GradingEdges.svelte";
  import FileViewer from "./FileViewer.svelte";
  import TruncatedStreamComponent from "./TruncatedStream.svelte";

  // Props
  interface Props {
    runId: string;
  }
  let { runId }: Props = $props();

  // State
  let run: AgentRunDetail | null = $state(null);
  let events: EventInfo[] = $state([]);
  let loading = $state(true);
  let pollInterval: ReturnType<typeof setInterval> | null = null;
  let expandedOutputs = $state(new SvelteSet<string>());

  // Critique viewer state
  let snapshotDetail: SnapshotDetailResponse | null = $state(null);
  let fileContents = $state(new SvelteMap<string, FileContentResponse>());
  let loadingSnapshot = $state(false);

  function toggleOutputExpanded(callId: string) {
    if (expandedOutputs.has(callId)) {
      expandedOutputs.delete(callId);
      expandedOutputs = new SvelteSet(expandedOutputs);
    } else {
      expandedOutputs = new SvelteSet([...expandedOutputs, callId]);
    }
  }

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
      const [runResult, eventsResult] = await Promise.all([fetchRun(runId), fetchRunEvents(runId, 0, 500)]);
      run = runResult;
      events = eventsResult.events;

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
    loadData();
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

  // Types for docker_exec handling
  type ExecStream = string | TruncatedStream;

  interface DockerExecPair {
    callEvent: EventInfo;
    outputEvent: EventInfo | null;
    input: ExecInput;
    result: BaseExecResult | null;
    callId: string;
  }

  // Type guards for typed payloads
  function isDockerExecCall(payload: EventInfo["payload"]): payload is DockerExecCallPayload {
    return payload.type === "docker_exec_call";
  }

  function isDockerExecOutput(payload: EventInfo["payload"]): payload is DockerExecOutputPayload {
    return payload.type === "docker_exec_output";
  }

  // Derive paired docker_exec events and remaining events
  const processedEvents = $derived.by(() => {
    const dockerExecPairs: DockerExecPair[] = [];
    const pairedCallIds = new SvelteSet<string>();

    // Build map of call_id -> output event
    const outputsByCallId = new SvelteMap<string, EventInfo>();
    for (const event of events) {
      if (isDockerExecOutput(event.payload)) {
        outputsByCallId.set(event.payload.call_id, event);
      }
    }

    // Find docker_exec calls and pair with outputs
    for (const event of events) {
      if (isDockerExecCall(event.payload)) {
        const callId = event.payload.call_id;
        const input = event.payload.input;
        const outputEvent = outputsByCallId.get(callId) || null;
        const result = outputEvent && isDockerExecOutput(outputEvent.payload) ? outputEvent.payload.result : null;

        dockerExecPairs.push({ callEvent: event, outputEvent, input, result, callId });
        pairedCallIds.add(callId);
      }
    }

    // Filter out paired events from main list
    const remainingEvents = events.filter((event) => {
      const payload = event.payload;
      if (isDockerExecCall(payload) && pairedCallIds.has(payload.call_id)) return false;
      if (isDockerExecOutput(payload) && pairedCallIds.has(payload.call_id)) return false;
      return true;
    });

    return { dockerExecPairs, remainingEvents };
  });

  // Unwrap /bin/sh -c "..." wrapper pattern
  function unwrapShellCommand(cmd: string[]): string[] {
    // Pattern: ["/bin/sh", "-c", "actual command..."]
    if (cmd.length === 3 && (cmd[0] === "/bin/sh" || cmd[0] === "sh") && cmd[1] === "-c") {
      // Return the inner command as a single element (it will be displayed as-is)
      return [cmd[2]];
    }
    return cmd;
  }

  // Format command for display
  function formatCommand(cmd: string[]): string {
    const unwrapped = unwrapShellCommand(cmd);
    // If unwrapped to single shell command string, display as-is
    if (unwrapped.length === 1 && cmd.length === 3 && cmd[1] === "-c") {
      return unwrapped[0];
    }
    // Quote args with spaces
    return unwrapped.map((arg) => (arg.includes(" ") ? `"${arg}"` : arg)).join(" ");
  }

  // Format duration - show in seconds if >= 1s and whole second
  function formatDuration(ms: number): string {
    if (ms >= 1000 && ms % 1000 === 0) {
      return `${ms / 1000}s`;
    }
    return `${ms}ms`;
  }

  // Get stream text (handles both string and TruncatedStream)
  function getStreamText(stream: ExecStream): string {
    if (typeof stream === "string") return stream;
    return stream.truncated_text;
  }

  // Format API usage stats
  function formatUsage(usage: {
    input_tokens?: number | null;
    output_tokens?: number | null;
    input_tokens_details?: { cached_tokens?: number | null } | null;
    output_tokens_details?: { reasoning_tokens?: number | null } | null;
  }): string {
    const cached = usage.input_tokens_details?.cached_tokens ?? 0;
    const reasoning = usage.output_tokens_details?.reasoning_tokens ?? 0;
    const parts = [`in: ${usage.input_tokens ?? 0}`, `out: ${usage.output_tokens ?? 0}`];
    if (cached > 0) parts.push(`cached: ${cached}`);
    if (reasoning > 0) parts.push(`reasoning: ${reasoning}`);
    return parts.join(", ");
  }

  // Format exit status
  function formatExitStatus(exit: BaseExecResult["exit"]): { text: string; color: string } {
    switch (exit.kind) {
      case "exited": {
        const code = exit.exit_code ?? 0;
        return {
          text: `exit ${code}`,
          color: code === 0 ? "text-green-600" : "text-red-600",
        };
      }
      case "timed_out":
        return { text: "TIMEOUT", color: "text-yellow-600" };
      case "killed":
        return { text: `killed (signal ${exit.signal})`, color: "text-red-600" };
    }
  }

  // Render event content based on type (for non-docker_exec events)
  function renderEventContent(event: EventInfo): {
    label: string;
    content: string;
    style: string;
    isMarkdown?: boolean;
  } {
    const payload = event.payload;

    switch (payload.type) {
      case "user_text":
        return { label: "User", content: payload.text, style: "bg-blue-50 border-blue-200" };
      case "assistant_text":
        return { label: "Assistant", content: payload.text, style: "bg-green-50 border-green-200" };
      case "tool_call": {
        const argsPreview = payload.args_json ? truncateText(payload.args_json) : "";
        return { label: `Tool: ${payload.name}`, content: argsPreview, style: "bg-purple-50 border-purple-200" };
      }
      case "tool_output": {
        const resultText = payload.content
          .map((c) => {
            if (typeof c === "object" && c !== null && "text" in c && typeof c.text === "string") {
              return c.text;
            }
            return "[non-text]";
          })
          .join("\n");
        const preview = truncateText(resultText, 200);
        return { label: "Tool Output", content: preview, style: "bg-gray-50 border-gray-200" };
      }
      case "reasoning": {
        const summaryText = payload.summary?.map((s) => s.text).join("\n");
        if (!summaryText)
          return { label: "Reasoning", content: "(thinking...)", style: "bg-yellow-50 border-yellow-200" };
        return { label: "Reasoning", content: summaryText, style: "bg-yellow-50 border-yellow-200", isMarkdown: true };
      }
      case "api_request":
        return { label: "API Request", content: `model: ${payload.model}`, style: "bg-indigo-50 border-indigo-200" };
      case "response":
        return { label: "Response", content: formatUsage(payload.usage), style: "bg-indigo-50 border-indigo-200" };
      default:
        return {
          label: payload.type,
          content: truncateText(JSON.stringify(payload)),
          style: "bg-gray-50 border-gray-200",
        };
    }
  }

  // Merge and sort events for display
  type DisplayEvent =
    | { kind: "docker_exec"; pair: DockerExecPair; seqNum: number }
    | { kind: "api_pair"; request: EventInfo; response: EventInfo; seqNum: number }
    | { kind: "regular"; event: EventInfo; seqNum: number };

  const displayEvents = $derived.by(() => {
    const items: DisplayEvent[] = [];

    // Add docker_exec pairs
    for (const pair of processedEvents.dockerExecPairs) {
      items.push({
        kind: "docker_exec",
        pair,
        seqNum: pair.callEvent.sequence_num,
      });
    }

    // Process remaining events, merging adjacent api_request + response
    const remaining = processedEvents.remainingEvents;
    const usedIndices = new SvelteSet<number>();

    for (let i = 0; i < remaining.length; i++) {
      if (usedIndices.has(i)) continue;

      const event = remaining[i];
      const payload = event.payload;

      // Check if this is api_request followed by response
      if (payload.type === "api_request" && i + 1 < remaining.length) {
        const nextEvent = remaining[i + 1];
        const nextPayload = nextEvent.payload;
        if (nextPayload.type === "response") {
          items.push({
            kind: "api_pair",
            request: event,
            response: nextEvent,
            seqNum: event.sequence_num,
          });
          usedIndices.add(i);
          usedIndices.add(i + 1);
          continue;
        }
      }

      items.push({
        kind: "regular",
        event,
        seqNum: event.sequence_num,
      });
    }

    // Sort by sequence number
    items.sort((a, b) => a.seqNum - b.seqNum);

    return items;
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
          <span class="text-gray-500">Events:</span>
          <span class="ml-1">{run.event_count}</span>
        </div>
        {#if run.parent_agent_run_id}
          <div>
            <span class="text-gray-500">Parent:</span>
            <span class="ml-1"><RunIdLink id={run.parent_agent_run_id} /></span>
          </div>
        {/if}
        {#if run.completion_summary}
          <div class="col-span-full flex items-start gap-1">
            <span class="text-gray-500 shrink-0">Summary:</span>
            <span class="prose prose-sm max-w-none inline"><SvelteMarkdown source={run.completion_summary} /></span>
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

    <!-- Events timeline -->
    <div class="p-4">
      <h3 class="text-md font-medium mb-3">Events ({events.length})</h3>
      {#if events.length === 0}
        <p class="text-gray-500 text-sm">No events yet</p>
      {:else}
        <div>
          {#each displayEvents as item, idx (item.seqNum)}
            {@const isFirst = idx === 0}
            {@const isLast = idx === displayEvents.length - 1}
            {@const roundingClass = isFirst && isLast ? "rounded" : isFirst ? "rounded-t" : isLast ? "rounded-b" : ""}
            {@const borderClass = isFirst ? "border" : "border-x border-b"}
            {#if item.kind === "docker_exec"}
              <!-- Docker Exec CLI-style display -->
              {@const pair = item.pair}
              {@const exitStatus = pair.result ? formatExitStatus(pair.result.exit) : null}
              <div
                class="{roundingClass} {borderClass} border-gray-700 bg-gray-900 text-gray-100 font-mono text-xs overflow-hidden"
              >
                <!-- Command header with cwd, command, and right-side info -->
                <div class="px-3 py-2 bg-gray-800 border-b border-gray-700 flex items-center justify-between">
                  <div class="flex items-center gap-2 min-w-0 flex-1">
                    {#if pair.input.cwd}
                      <span class="text-gray-400 shrink-0">{pair.input.cwd}</span>
                    {/if}
                    <span class="text-green-400 shrink-0">$</span>
                    <span class="text-white truncate">{formatCommand(pair.input.cmd)}</span>
                  </div>
                  <div class="flex items-center gap-3 text-[10px] shrink-0 ml-2">
                    {#if exitStatus}
                      <span class={exitStatus.color}>{exitStatus.text}</span>
                    {/if}
                    <span class="text-gray-500 flex items-center gap-1">
                      <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"
                        ><circle cx="12" cy="12" r="10" stroke-width="2" /><path
                          stroke-width="2"
                          d="M12 6v6l4 2"
                        /></svg
                      >
                      {#if pair.result}
                        {formatDuration(pair.result.duration_ms)}/{formatDuration(pair.input.timeout_ms)}
                      {:else}
                        —/{formatDuration(pair.input.timeout_ms)}
                      {/if}
                    </span>
                    <span class="text-gray-500">#{item.seqNum}</span>
                  </div>
                </div>

                <!-- Extra parameters (if any non-default, excluding cwd which is now in header) -->
                {#if pair.input.env?.length || pair.input.user}
                  <div
                    class="px-3 py-1 bg-gray-850 border-b border-gray-700 text-gray-400 text-[10px] flex flex-wrap gap-3"
                  >
                    {#if pair.input.user}
                      <span>user: {pair.input.user}</span>
                    {/if}
                    {#if pair.input.env?.length}
                      <span>env: {pair.input.env.length} vars</span>
                    {/if}
                  </div>
                {/if}

                <!-- Output (expandable) -->
                {#if pair.result}
                  {@const callId = pair.callId}
                  {@const isExpanded = expandedOutputs.has(callId)}
                  {@const stdoutText = getStreamText(pair.result.stdout)}
                  {@const stderrText = getStreamText(pair.result.stderr)}
                  {@const outputLines = (stdoutText + stderrText).split("\n").length}
                  {@const needsExpand = outputLines > 8}

                  <div class="relative">
                    <div class="px-3 py-2 {needsExpand && !isExpanded ? 'max-h-48 overflow-y-auto' : ''}">
                      <TruncatedStreamComponent stream={pair.result.stdout} kind="stdout" />
                      <TruncatedStreamComponent stream={pair.result.stderr} kind="stderr" />
                      {#if !stdoutText && !stderrText}
                        <span class="text-gray-500 italic">(no output)</span>
                      {/if}
                    </div>
                    <!-- Expand/collapse toggle overlay -->
                    {#if needsExpand}
                      <button
                        type="button"
                        onclick={() => toggleOutputExpanded(callId)}
                        class="absolute bottom-0 left-0 right-0 h-6 bg-gradient-to-t from-gray-900 to-transparent flex items-end justify-center pb-1 cursor-pointer hover:from-gray-800 transition-colors"
                      >
                        <span class="text-[10px] text-gray-400">{isExpanded ? "▲ collapse" : "▼ expand"}</span>
                      </button>
                    {/if}
                  </div>
                {:else}
                  <div class="px-3 py-2 text-gray-500 italic">(awaiting result...)</div>
                {/if}
              </div>
            {:else if item.kind === "api_pair"}
              <!-- Merged API Request + Response -->
              {@const reqPayload = item.request.payload}
              {@const respPayload = item.response.payload}
              <div class="p-2 {roundingClass} {borderClass} bg-indigo-50 border-indigo-200">
                <div class="text-xs">
                  <span class="float-right text-gray-400 ml-2">#{item.seqNum}</span>
                  <span class="font-medium">API Request</span>
                  {#if reqPayload.type === "api_request"}
                    <span class="font-mono ml-1">model: {reqPayload.model}</span>
                  {/if}
                  <span class="mx-2 text-gray-400">→</span>
                  <span class="font-medium">Response</span>
                  {#if respPayload.type === "response"}
                    <span class="font-mono ml-1">{formatUsage(respPayload.usage)}</span>
                  {/if}
                </div>
              </div>
            {:else}
              <!-- Regular event -->
              {@const rendered = renderEventContent(item.event)}
              {@const payload = item.event.payload}
              {@const isInline = payload.type === "reasoning"}
              <div class="p-2 {roundingClass} {borderClass} {rendered.style}">
                {#if isInline}
                  <!-- Inline layout: label + content on one line, seqNum floated right -->
                  <div class="text-xs">
                    <span class="float-right text-gray-400 ml-2">#{item.seqNum}</span>
                    <span class="font-medium">{rendered.label}</span>
                    {#if rendered.isMarkdown}
                      <span class="prose prose-sm max-w-none inline"><SvelteMarkdown source={rendered.content} /></span>
                    {:else}
                      <span class="font-mono ml-1">{rendered.content}</span>
                    {/if}
                  </div>
                {:else}
                  <!-- Block layout: header on top, content below -->
                  <div class="flex items-center justify-between mb-1">
                    <span class="text-xs font-medium">{rendered.label}</span>
                    <span class="text-xs text-gray-400">#{item.seqNum}</span>
                  </div>
                  {#if rendered.isMarkdown}
                    <div class="text-xs prose prose-sm max-w-none"><SvelteMarkdown source={rendered.content} /></div>
                  {:else}
                    <pre class="text-xs whitespace-pre-wrap break-words font-mono">{rendered.content}</pre>
                  {/if}
                {/if}
              </div>
            {/if}
          {/each}
        </div>
      {/if}
    </div>
  {:else}
    <div class="p-4">
      <p class="text-red-500">Failed to load run</p>
    </div>
  {/if}
</div>
