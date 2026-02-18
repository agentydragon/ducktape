<script lang="ts">
  import { getContext } from "svelte";
  import { searchParams } from "$lib/router";
  import RunsBrowser from "$components/RunsBrowser.svelte";
  import type { RunModalPrefill, RunTrigger, Split, ExampleKind } from "$lib/types";

  const runModal = getContext<{
    open: (_?: RunModalPrefill) => void;
  }>("runModal");

  const definitionId = $derived($searchParams.get("definition") ?? undefined);
  const split = $derived($searchParams.get("split") as Split | undefined);
  const kind = $derived($searchParams.get("kind") as ExampleKind | undefined);

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
