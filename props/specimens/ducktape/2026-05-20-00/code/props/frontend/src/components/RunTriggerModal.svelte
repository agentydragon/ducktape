<script lang="ts">
  import { untrack } from "svelte";
  import { toast } from "svelte-sonner";
  import { resolve } from "$lib/router";
  import { formatDigest } from "$lib/formatters";
  import {
    api,
    fetchDefinitions,
    fetchModelMetadata,
    triggerValidationRuns,
    type DefinitionInfo,
    type ModelMetadataInfo,
    type Split,
    type ExampleKind,
  } from "../lib/api/client";

  interface Prefill {
    definitionId?: string;
    split?: Split;
    kind?: ExampleKind;
  }

  interface Props {
    open: boolean;
    onClose: () => void;
    prefill?: Prefill;
  }

  let { open, onClose, prefill }: Props = $props();

  type RunMode = "validation" | "optimize" | "improve";
  let mode: RunMode = $state("validation");

  // Shared state
  let loading = $state(false);
  let loadingDefinitions = $state(true);
  let loadingModels = $state(true);
  let definitions: DefinitionInfo[] = $state([]);
  let modelIds: string[] = $state([]);
  let resultMessage: string | null = $state(null);
  let resultRunId: string | null = $state(null);

  // Shared across all modes
  let budgetUsd: number = $state(5.0);
  let timeoutSeconds: number = $state(3600);
  let criticModel: string = $state("gpt-5.1-codex-mini");

  // Validation-specific
  let selectedDefinition: string = $state("");
  let selectedSplit: Split = $state("valid");
  let selectedKind: ExampleKind = $state("whole_snapshot");
  let nSamples: number = $state(5);

  // Optimize-specific
  let optTargetMetric: string = $state("whole-repo");
  let optOptimizerModel: string = $state("gpt-5.1");

  // Improve-specific
  let impNExamples: number = $state(10);
  let impImprovementModel: string = $state("gpt-5.1");

  let dataFetched = false;

  // Fetch definitions and models on first open
  $effect(() => {
    if (open && !dataFetched) {
      dataFetched = true;
      untrack(async () => {
        try {
          const [defResult, modelResult] = await Promise.all([fetchDefinitions("critic"), fetchModelMetadata()]);
          definitions = defResult.definitions;
          if (definitions.length > 0 && !selectedDefinition) {
            selectedDefinition = definitions[0].image_digest;
          }
          modelIds = modelResult.models.map((m: ModelMetadataInfo) => m.model_id);
        } catch (e) {
          const message = e instanceof Error ? e.message : "Failed to load data";
          toast.error(message);
        } finally {
          loadingDefinitions = false;
          loadingModels = false;
        }
      });
    }
  });

  let prefillApplied = false;

  $effect(() => {
    if (open && prefill && !prefillApplied) {
      prefillApplied = true;
      if (prefill.definitionId) selectedDefinition = prefill.definitionId;
      if (prefill.split) selectedSplit = prefill.split;
      if (prefill.kind) selectedKind = prefill.kind;
    }
    if (!open) {
      prefillApplied = false;
    }
  });

  function clearResult() {
    resultMessage = null;
    resultRunId = null;
  }

  async function handleValidation() {
    if (!selectedDefinition) return;
    loading = true;
    clearResult();
    try {
      const result = await triggerValidationRuns({
        image_digest: selectedDefinition,
        split: selectedSplit,
        example_kind: selectedKind,
        n_samples: nSamples,
        critic_model: criticModel,
        budget_usd: budgetUsd,
      });
      resultMessage = `${result.message} (job ${result.job_id.slice(0, 8)})`;
      toast.success(resultMessage);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to trigger validation runs");
    } finally {
      loading = false;
    }
  }

  async function handleOptimize() {
    loading = true;
    clearResult();
    try {
      const { data, error } = await api.POST("/api/runs/optimize", {
        body: {
          target_metric: optTargetMetric as "whole-repo" | "targeted",
          budget_usd: budgetUsd,
          optimizer_model: optOptimizerModel,
          critic_model: criticModel,
          timeout_seconds: timeoutSeconds,
        },
      });
      if (error) throw new Error((error as { detail?: string }).detail ?? "Failed to launch optimize agent");
      resultRunId = data.agent_run_id;
      resultMessage = `Optimize agent launched`;
      toast.success(resultMessage);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to launch optimize agent");
    } finally {
      loading = false;
    }
  }

  async function handleImprove() {
    loading = true;
    clearResult();
    try {
      const { data, error } = await api.POST("/api/runs/improve", {
        body: {
          n_examples: impNExamples,
          budget_usd: budgetUsd,
          improvement_model: impImprovementModel,
          critic_model: criticModel,
          timeout_seconds: timeoutSeconds,
        },
      });
      if (error) throw new Error((error as { detail?: string }).detail ?? "Failed to launch improve agent");
      resultRunId = data.agent_run_id;
      resultMessage = `Improve agent launched on ${data.n_examples_selected} examples from ${formatDigest(data.definition_id)}`;
      toast.success(resultMessage);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to launch improve agent");
    } finally {
      loading = false;
    }
  }

  async function handleTrigger() {
    if (mode === "validation") await handleValidation();
    else if (mode === "optimize") await handleOptimize();
    else if (mode === "improve") await handleImprove();
  }

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) onClose();
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === "Escape") onClose();
  }

  const inputClass =
    "w-full border dark:border-gray-600 rounded px-3 py-2 text-sm bg-white dark:bg-gray-700 focus:ring-2 focus:ring-blue-500 focus:border-blue-500";

  const tabs: { key: RunMode; label: string }[] = [
    { key: "validation", label: "Validation" },
    { key: "optimize", label: "Optimize" },
    { key: "improve", label: "Improve" },
  ];
</script>

{#snippet modelSelect(id: string, label: string, value: string, onchange: (v: string) => void)}
  <div>
    <label for={id} class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">{label}</label>
    <select
      {id}
      class={inputClass}
      disabled={loading || loadingModels}
      {value}
      onchange={(e) => onchange(e.currentTarget.value)}
    >
      {#each modelIds as modelId (modelId)}
        <option value={modelId}>{modelId}</option>
      {/each}
    </select>
  </div>
{/snippet}

{#if open}
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div
    class="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
    role="dialog"
    aria-modal="true"
    aria-labelledby="modal-title"
    tabindex="-1"
    onclick={handleBackdropClick}
    onkeydown={handleKeydown}
  >
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="bg-white dark:bg-gray-800 rounded-lg shadow-xl dark:shadow-gray-950/50 p-6 w-full max-w-md max-h-[90vh] overflow-y-auto"
      role="document"
      onclick={(e) => e.stopPropagation()}
      onkeydown={() => {}}
    >
      <h2 id="modal-title" class="text-lg font-semibold mb-3">Launch Agent</h2>

      <!-- Shared: Budget & Timeout -->
      <div class="grid grid-cols-2 gap-3 mb-4">
        <div>
          <label for="s-budget" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >Budget ($)</label
          >
          <input
            id="s-budget"
            type="number"
            bind:value={budgetUsd}
            min="0.01"
            step="0.5"
            class={inputClass}
            disabled={loading}
          />
        </div>
        <div>
          <label for="s-timeout" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >Timeout (s)</label
          >
          <input
            id="s-timeout"
            type="number"
            bind:value={timeoutSeconds}
            min="60"
            step="300"
            class={inputClass}
            disabled={loading}
          />
        </div>
      </div>

      <!-- Shared: Critic Model -->
      {@render modelSelect("s-critic", "Critic Model", criticModel, (v) => (criticModel = v))}

      <!-- Mode tabs -->
      <div class="flex border-b border-gray-200 dark:border-gray-700 mb-4 mt-4">
        {#each tabs as tab (tab.key)}
          <button
            type="button"
            class="px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors
              {mode === tab.key
              ? 'border-blue-600 dark:border-blue-400 text-blue-600 dark:text-blue-400'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:border-gray-300 dark:hover:border-gray-600'}"
            onclick={() => {
              mode = tab.key;
              clearResult();
            }}
          >
            {tab.label}
          </button>
        {/each}
      </div>

      {#if loadingDefinitions && mode === "validation"}
        <p class="text-gray-500 dark:text-gray-400">Loading definitions...</p>
      {:else}
        <div class="space-y-3">
          {#if mode === "validation"}
            <!-- Validation form -->
            <div>
              <label for="m-def" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >Critic Definition</label
              >
              <select id="m-def" bind:value={selectedDefinition} class={inputClass} disabled={loading}>
                {#each definitions as def (def.image_digest)}
                  <option value={def.image_digest}>{formatDigest(def.image_digest)}</option>
                {/each}
              </select>
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label for="m-split" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                  >Split</label
                >
                <select id="m-split" bind:value={selectedSplit} class={inputClass} disabled={loading}>
                  <option value="train">Train</option>
                  <option value="valid">Validation</option>
                </select>
              </div>
              <div>
                <label for="m-kind" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                  >Example Kind</label
                >
                <select id="m-kind" bind:value={selectedKind} class={inputClass} disabled={loading}>
                  <option value="whole_snapshot">Whole Snapshot</option>
                  <option value="file_set">File Set</option>
                </select>
              </div>
            </div>
            <div>
              <label for="m-n" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >Samples (1-50)</label
              >
              <input
                id="m-n"
                type="number"
                bind:value={nSamples}
                min="1"
                max="50"
                class={inputClass}
                disabled={loading}
              />
            </div>
          {:else if mode === "optimize"}
            <!-- Optimize form -->
            <div>
              <label for="o-metric" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >Target Metric</label
              >
              <select id="o-metric" bind:value={optTargetMetric} class={inputClass} disabled={loading}>
                <option value="whole-repo">Whole Repo (full-snapshot validation only)</option>
                <option value="targeted">Targeted (includes per-file validation)</option>
              </select>
            </div>
            {@render modelSelect("o-optmodel", "Optimizer Model", optOptimizerModel, (v) => (optOptimizerModel = v))}
          {:else if mode === "improve"}
            <!-- Improve form -->
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
              Auto-selects the best definition (by validation LCB) and top Pareto training examples.
            </p>
            {@render modelSelect(
              "i-impmodel",
              "Improvement Model",
              impImprovementModel,
              (v) => (impImprovementModel = v)
            )}
            <div>
              <label for="i-n" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Examples</label>
              <input
                id="i-n"
                type="number"
                bind:value={impNExamples}
                min="1"
                max="100"
                class={inputClass}
                disabled={loading}
              />
            </div>
          {/if}
        </div>

        <!-- Result message -->
        {#if resultMessage}
          <div class="mt-3 text-sm text-green-700 dark:text-green-300 bg-green-50 dark:bg-green-950 p-2 rounded">
            {resultMessage}
            {#if resultRunId}
              — <a href={resolve(`/runs/${resultRunId}`)} class="underline font-medium" onclick={onClose}>view run</a>
            {/if}
          </div>
        {/if}

        <!-- Buttons -->
        <div class="flex justify-end gap-3 mt-4">
          <button
            type="button"
            onclick={onClose}
            disabled={loading}
            class="px-4 py-2 text-sm border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 rounded hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
          >
            {resultMessage ? "Close" : "Cancel"}
          </button>
          <button
            type="button"
            onclick={handleTrigger}
            disabled={loading || (mode === "validation" && (!selectedDefinition || budgetUsd <= 0))}
            class="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300 dark:disabled:bg-gray-600 disabled:cursor-not-allowed"
          >
            {loading ? "Launching..." : mode === "validation" ? "Run" : mode === "optimize" ? "Optimize" : "Improve"}
          </button>
        </div>
      {/if}
    </div>
  </div>
{/if}
