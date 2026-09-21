import RunsBrowser from "$components/RunsBrowser";
import { useSearchParams } from "$lib/router";
import { useRunModal } from "$lib/runModalContext";
import type { ExampleKind, RunTrigger, Split } from "$lib/types";

export default function RunsPage() {
  const searchParams = useSearchParams();
  const runModal = useRunModal();
  const definitionId = searchParams.get("definition") ?? undefined;
  const split = searchParams.get("split") as Split | undefined;
  const kind = searchParams.get("kind") as ExampleKind | undefined;

  function handleTriggerRun(prefill: RunTrigger) {
    runModal.open(prefill);
  }

  return (
    <RunsBrowser
      initialDefinitionId={definitionId}
      initialSplit={split}
      initialKind={kind}
      onTriggerRun={handleTriggerRun}
    />
  );
}
