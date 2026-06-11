<script lang="ts">
  import { onMount } from "svelte";
  import { toast } from "svelte-sonner";
  import {
    fetchDefinitions,
    triggerValidationRuns,
    type DefinitionInfo,
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

  // Form state
  let definitions: DefinitionInfo[] = $state([]);
  let selectedDefinition: string = $state("");
  let selectedSplit: Split = $state("valid");
  let selectedKind: ExampleKind = $state("whole_snapshot");
  let nSamples: number = $state(5);
  let loading = $state(false);
  let loadingDefinitions = $state(true);

  // Load definitions on mount
  onMount(async () => {
    try {
      const result = await fetchDefinitions("critic");
      definitions = result.definitions;
      if (definitions.length > 0 && !selectedDefinition) {
        selectedDefinition = definitions[0].image_digest;
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : "Failed to load definitions";
      toast.error(message);
    } finally {
      loadingDefinitions = false;
    }
  });

  // Apply prefill when it changes
  $effect(() => {
    if (prefill) {
      if (prefill.definitionId) selectedDefinition = prefill.definitionId;
      if (prefill.split) selectedSplit = prefill.split;
      if (prefill.kind) selectedKind = prefill.kind;
    }
  });

  async function handleTrigger() {
    if (!selectedDefinition) return;

    loading = true;

    try {
      const result = await triggerValidationRuns({
        image_digest: selectedDefinition,
        split: selectedSplit,
        example_kind: selectedKind,
        n_samples: nSamples,
        critic_model: "gpt-5.1-codex-mini",
        grader_model: "gpt-5.1-codex-mini",
      });
      toast.success(result.message);
      onClose();
    } catch (e) {
      const message = e instanceof Error ? e.message : "Failed to trigger runs";
      toast.error(message);
    } finally {
      loading = false;
    }
  }

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) {
      onClose();
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      onClose();
    }
  }
</script>

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
      class="bg-white rounded-lg shadow-xl p-6 w-full max-w-md"
      role="document"
      onclick={(e) => e.stopPropagation()}
      onkeydown={() => {}}
    >
      <h2 id="modal-title" class="text-lg font-semibold mb-4">Trigger Runs</h2>

      {#if loadingDefinitions}
        <p class="text-gray-500">Loading definitions...</p>
      {:else}
        <div class="space-y-4">
          <!-- Definition selector -->
          <div>
            <label for="modal-definition" class="block text-sm font-medium text-gray-700 mb-1">
              Critic Definition
            </label>
            <select
              id="modal-definition"
              bind:value={selectedDefinition}
              class="w-full border rounded px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              disabled={loading}
            >
              {#each definitions as def (def.image_digest)}
                <option value={def.image_digest}>{def.image_digest}</option>
              {/each}
            </select>
          </div>

          <!-- Split selector -->
          <div>
            <label for="modal-split" class="block text-sm font-medium text-gray-700 mb-1"> Split </label>
            <select
              id="modal-split"
              bind:value={selectedSplit}
              class="w-full border rounded px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              disabled={loading}
            >
              <option value="train">Train</option>
              <option value="valid">Validation</option>
            </select>
          </div>

          <!-- Example kind -->
          <div>
            <label for="modal-kind" class="block text-sm font-medium text-gray-700 mb-1"> Example Kind </label>
            <select
              id="modal-kind"
              bind:value={selectedKind}
              class="w-full border rounded px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              disabled={loading}
            >
              <option value="whole_snapshot">Whole Snapshot</option>
              <option value="file_set">File Set</option>
            </select>
          </div>

          <!-- Number of samples -->
          <div>
            <label for="modal-samples" class="block text-sm font-medium text-gray-700 mb-1">
              Number of Samples (1-50)
            </label>
            <input
              id="modal-samples"
              type="number"
              bind:value={nSamples}
              min="1"
              max="50"
              class="w-full border rounded px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              disabled={loading}
            />
          </div>
        </div>

        <!-- Buttons -->
        <div class="flex justify-end gap-3 mt-6">
          <button
            type="button"
            onclick={onClose}
            disabled={loading}
            class="px-4 py-2 text-sm border border-gray-300 text-gray-700 bg-white rounded hover:bg-gray-50 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onclick={handleTrigger}
            disabled={loading || !selectedDefinition}
            class="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
          >
            {loading ? "Running..." : "Run"}
          </button>
        </div>
      {/if}
    </div>
  </div>
{/if}
