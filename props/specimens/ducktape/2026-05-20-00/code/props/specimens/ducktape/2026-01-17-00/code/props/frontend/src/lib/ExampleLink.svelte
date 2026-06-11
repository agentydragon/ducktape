<script lang="ts">
  // Link component for examples
  // Uses native <a> with SvelteKit client-side navigation

  import { resolve } from "$lib/router";
  import { SvelteURLSearchParams } from "svelte/reactivity";
  import { formatSnapshotSlug } from "./formatters";
  import type { WholeSnapshotExample, SingleFileSetExample } from "./api/client";

  type Example = WholeSnapshotExample | SingleFileSetExample;

  interface Props {
    example: Example;
  }

  let { example }: Props = $props();

  const displayText = $derived(
    example.kind === "whole_snapshot"
      ? `whole@${formatSnapshotSlug(example.snapshot_slug)}`
      : `files@${formatSnapshotSlug(example.snapshot_slug)}/${example.files_hash.slice(0, 6)}`
  );

  const queryString = $derived.by(() => {
    const params = new SvelteURLSearchParams({
      snapshot_slug: example.snapshot_slug,
      example_kind: example.kind,
    });
    if (example.kind === "file_set") {
      params.set("files_hash", example.files_hash);
    }
    return params.toString();
  });
</script>

<a
  href={resolve(`/examples?${queryString}`)}
  class="font-mono text-xs text-blue-600 underline hover:text-blue-800"
  title="{example.snapshot_slug} ({example.kind})"
>
  {displayText}
</a>
