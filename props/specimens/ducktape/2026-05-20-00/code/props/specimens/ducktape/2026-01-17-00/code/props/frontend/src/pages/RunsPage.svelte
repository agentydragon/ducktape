<script lang="ts">
  import { getContext } from "svelte";
  import { pathname } from "$lib/router";
  import RunsBrowser from "$components/RunsBrowser.svelte";
  import type { RunModalPrefill, RunTrigger, Split, ExampleKind } from "$lib/types";

  const runModal = getContext<{
    open: (_?: RunModalPrefill) => void;
  }>("runModal");

  // Parse query params from hash URL
  // Format: #/runs?definition=xxx&split=xxx&kind=xxx
  const queryParams = $derived.by(() => {
    const path = $pathname;
    const queryStart = path.indexOf("?");
    if (queryStart === -1) return new URLSearchParams();
    return new URLSearchParams(path.slice(queryStart + 1));
  });

  const definitionId = $derived(queryParams.get("definition") ?? undefined);
  const split = $derived(queryParams.get("split") as Split | undefined);
  const kind = $derived(queryParams.get("kind") as ExampleKind | undefined);

  function handleTriggerRun(prefill: RunTrigger) {
    runModal?.open(prefill);
  }
</script>

<RunsBrowser
  initialDefinitionId={definitionId}
  initialSplit={split}
  initialKind={kind}
  onTriggerRun={handleTriggerRun}
/>
