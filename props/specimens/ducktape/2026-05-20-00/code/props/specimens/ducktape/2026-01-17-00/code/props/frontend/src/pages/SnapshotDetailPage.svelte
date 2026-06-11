<script lang="ts">
  import { onMount } from "svelte";
  import { pathname, resolve } from "$lib/router";
  import { toast } from "svelte-sonner";
  import { splitBadgeClass } from "$lib/colors";
  import type { SnapshotDetailResponse, FileContentResponse, FileTreeResponse } from "$lib/api/client";
  import { fetchSnapshotDetail, fetchSnapshotTree, fetchSnapshotFile } from "$lib/api/client";
  import FileTree from "$components/FileTree.svelte";
  import FileViewer from "$components/FileViewer.svelte";
  import TabButton from "$components/TabButton.svelte";
  import Breadcrumb from "$components/Breadcrumb.svelte";
  import CopyButton from "$components/CopyButton.svelte";
  import BackButton from "$components/BackButton.svelte";
  import OccurrenceLink from "$lib/OccurrenceLink.svelte";
  import { createExpansionState } from "$lib/expansionState.svelte";

  interface Props {
    slug: string; // This is the full catch-all path: "snapshot-name" or "snapshot-name/issueId/occurrenceId"
    initialSnapshot?: SnapshotDetailResponse;
    initialTree?: FileTreeResponse;
  }
  let { slug, initialSnapshot, initialTree }: Props = $props();

  // Parse the slug into components
  const parsedSlug = $derived.by(() => {
    const parts = slug.split("/");
    const snapshotSlug = parts[0];
    const issueId = parts.length >= 3 ? parts[1] : undefined;
    const occurrenceId = parts.length >= 3 ? parts[2] : undefined;
    return { snapshotSlug, issueId, occurrenceId };
  });

  // Parse query params for file
  const queryParams = $derived.by(() => {
    const path = $pathname;
    const queryStart = path.indexOf("?");
    if (queryStart === -1) return new URLSearchParams();
    return new URLSearchParams(path.slice(queryStart + 1));
  });
  const targetFile = $derived(queryParams.get("file") || undefined);

  let snapshot: SnapshotDetailResponse | null = $state(initialSnapshot ?? null);
  let tree: FileTreeResponse | null = $state(initialTree ?? null);
  let loading = $state(!initialSnapshot);
  let error: string | null = $state(null);

  const expandedIssues = createExpansionState();
  let activeTab: "files" | "tps" | "fps" = $state("files");
  let selectedFile: FileContentResponse | null = $state(null);
  let loadingFile = $state(false);

  async function loadData() {
    loading = true;
    error = null;
    try {
      const [snapshotData, treeData] = await Promise.all([
        fetchSnapshotDetail(parsedSlug.snapshotSlug),
        fetchSnapshotTree(parsedSlug.snapshotSlug),
      ]);
      snapshot = snapshotData;
      tree = treeData;
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load snapshot";
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    if (!initialSnapshot) {
      loadData();
    }
  });

  // Reload when slug changes
  $effect(() => {
    if (parsedSlug.snapshotSlug) {
      loadData();
    }
  });

  // Breadcrumb items for file viewer
  const breadcrumbs = $derived.by(() => {
    if (!selectedFile || !snapshot) return [{ label: parsedSlug.snapshotSlug }];

    const parts = selectedFile.path.split("/");
    const items: Array<{ label: string; href?: string }> = [
      { label: snapshot.slug, href: `/snapshots/${parsedSlug.snapshotSlug}` },
      ...parts.map((part) => ({ label: part })),
    ];

    return items;
  });

  function formatFileLocation(file: {
    path: string;
    ranges: Array<{ start_line: number; end_line?: number | null; note?: string | null }> | null;
  }): string {
    if (!file.ranges || file.ranges.length === 0) {
      return file.path;
    }
    const rangeStrs = file.ranges.map((r) => {
      const endLine = r.end_line ?? r.start_line;
      return r.start_line === endLine ? `${r.start_line}` : `${r.start_line}-${endLine}`;
    });
    return `${file.path}:${rangeStrs.join(",")}`;
  }

  async function handleFileClick(path: string) {
    loadingFile = true;
    try {
      selectedFile = await fetchSnapshotFile(parsedSlug.snapshotSlug, path);
    } catch (err) {
      toast.error(`Failed to load file: ${err}`);
    } finally {
      loadingFile = false;
    }
  }

  // Generate URL for occurrence
  function getOccurrenceUrl(issueId: string, occurrenceId: string, filePath?: string): string {
    const routePath = `/snapshots/${parsedSlug.snapshotSlug}/${issueId}/${occurrenceId}`;
    const hashPath = resolve(routePath);
    if (filePath) {
      return `${window.location.origin}/${hashPath}?file=${encodeURIComponent(filePath)}`;
    }
    return `${window.location.origin}/${hashPath}`;
  }

  function findAndNavigateToOccurrence(issueId: string, occurrenceId: string, filePath?: string) {
    if (!snapshot) return;
    const snap = snapshot; // Capture for closure

    const searchInIssues = (issues: typeof snap.true_positives | typeof snap.false_positives) => {
      for (const issue of issues) {
        const issueIdMatch = "tp_id" in issue ? issue.tp_id === issueId : issue.fp_id === issueId;
        if (issueIdMatch) {
          const occ = issue.occurrences.find((o) => o.occurrence_id === occurrenceId);
          if (occ?.files.length) {
            // Use specified file or first file
            const fileToLoad = filePath || occ.files[0].path;
            handleFileClick(fileToLoad);
            expandedIssues.expand(issueId);
            activeTab = "files";
            setTimeout(() => {
              document.getElementById(`${issueId}-${occurrenceId}`)?.scrollIntoView({
                behavior: "smooth",
                block: "center",
              });
            }, 100);
            return true;
          }
        }
      }
      return false;
    };

    searchInIssues(snap.true_positives) || searchInIssues(snap.false_positives);
  }

  // Handle deep linking via route params
  $effect(() => {
    if (parsedSlug.issueId && parsedSlug.occurrenceId && snapshot) {
      findAndNavigateToOccurrence(parsedSlug.issueId, parsedSlug.occurrenceId, targetFile);
    }
  });
</script>

{#if loading}
  <div class="flex items-center justify-center py-12">
    <div class="text-gray-500">Loading...</div>
  </div>
{:else if error}
  <div class="bg-red-50 border border-red-200 rounded p-4 text-red-700">
    {error}
  </div>
{:else if snapshot && tree}
  <div class="bg-white rounded-lg shadow">
    <!-- Header -->
    <div class="px-4 py-3 border-b">
      <div class="flex justify-between items-center mb-2">
        <div class="flex items-center gap-3">
          <BackButton href="/snapshots" />
          <h2 class="text-xl font-semibold font-mono">{snapshot.slug}</h2>
          <span class="px-2 py-1 text-xs font-medium rounded {splitBadgeClass(snapshot.split)}">
            {snapshot.split}
          </span>
        </div>
      </div>
      <Breadcrumb
        items={[{ label: "Home", href: "/" }, { label: "Snapshots", href: "/snapshots" }, { label: snapshot.slug }]}
      />
    </div>

    <!-- Tabs -->
    <div class="border-b">
      <nav class="flex -mb-px">
        <TabButton active={activeTab === "files"} onclick={() => (activeTab = "files")}>Files</TabButton>
        <TabButton active={activeTab === "tps"} onclick={() => (activeTab = "tps")}>
          True Positives ({snapshot.true_positives.length})
        </TabButton>
        <TabButton active={activeTab === "fps"} onclick={() => (activeTab = "fps")}>
          False Positives ({snapshot.false_positives.length})
        </TabButton>
      </nav>
    </div>

    <!-- Content -->
    <div class="p-4">
      {#if activeTab === "files"}
        <div class="grid grid-cols-2 gap-4">
          <!-- File Tree -->
          <div class="overflow-y-auto max-h-[70vh]">
            <h3 class="text-sm font-medium mb-2">File Browser</h3>
            <FileTree nodes={tree.tree} onFileClick={handleFileClick} selectedPath={selectedFile?.path} />
          </div>

          <!-- File Viewer -->
          <div class="overflow-y-auto max-h-[70vh]">
            {#if loadingFile}
              <div class="flex items-center justify-center h-full text-gray-500">Loading...</div>
            {:else if selectedFile}
              <div class="mb-3">
                <Breadcrumb items={breadcrumbs} />
              </div>
              <FileViewer
                file={selectedFile}
                tps={snapshot.true_positives}
                fps={snapshot.false_positives}
                snapshotSlug={snapshot.slug}
                targetOccurrenceId={parsedSlug.occurrenceId}
              />
            {:else}
              <div class="flex items-center justify-center h-full text-gray-500">Select a file to view</div>
            {/if}
          </div>
        </div>
      {:else if activeTab === "tps"}
        <div class="max-h-[70vh] overflow-y-auto">
          {#if snapshot.true_positives.length === 0}
            <p class="text-gray-500">No true positives</p>
          {:else}
            <div class="space-y-2">
              {#each snapshot.true_positives as tp (tp.tp_id)}
                <div class="border rounded">
                  <button
                    class="w-full px-3 py-2 flex justify-between items-center hover:bg-gray-50 text-left"
                    onclick={() => expandedIssues.toggle(tp.tp_id)}
                  >
                    <div class="flex items-center gap-2">
                      <span class="text-gray-400">{expandedIssues.isExpanded(tp.tp_id) ? "▼" : "▶"}</span>
                      <span class="font-mono text-sm font-medium">{tp.tp_id}</span>
                      <span class="text-xs text-gray-500">({tp.occurrences.length} occ)</span>
                    </div>
                  </button>

                  {#if expandedIssues.isExpanded(tp.tp_id)}
                    <div class="px-3 pb-3 border-t bg-gray-50">
                      <div class="mt-2">
                        <h4 class="text-xs font-medium text-gray-500 uppercase mb-1">Rationale</h4>
                        <p class="text-sm whitespace-pre-wrap">{tp.rationale}</p>
                      </div>
                      <div class="mt-3">
                        <h4 class="text-xs font-medium text-gray-500 uppercase mb-1">Occurrences</h4>
                        {#each tp.occurrences as occ (occ.occurrence_id)}
                          <div
                            id="{tp.tp_id}-{occ.occurrence_id}"
                            class="bg-white border rounded p-2 mt-1 {parsedSlug.occurrenceId === occ.occurrence_id
                              ? 'ring-2 ring-blue-500'
                              : ''}"
                          >
                            <div class="flex items-center justify-between">
                              <div class="text-xs">
                                <OccurrenceLink
                                  snapshotSlug={snapshot.slug}
                                  issueId={tp.tp_id}
                                  occurrenceId={occ.occurrence_id}
                                  filePath={occ.files[0]?.path}
                                />
                              </div>
                              <CopyButton
                                text={getOccurrenceUrl(tp.tp_id, occ.occurrence_id, occ.files[0]?.path)}
                                label="Copy URL"
                              />
                            </div>
                            <div class="mt-1">
                              {#each occ.files as file (`${occ.occurrence_id}-${file.path}`)}
                                <div class="text-sm font-mono">{formatFileLocation(file)}</div>
                              {/each}
                            </div>
                            {#if occ.note}
                              <div class="mt-1 text-sm text-gray-600 italic">{occ.note}</div>
                            {/if}
                            {#if occ.critic_scopes_expected_to_recall && occ.critic_scopes_expected_to_recall.length > 0}
                              <div class="mt-1 text-xs text-gray-500">
                                Expected recall scopes: {occ.critic_scopes_expected_to_recall
                                  .map((f: string[]) => f.join(", "))
                                  .join(" | ")}
                              </div>
                            {/if}
                          </div>
                        {/each}
                      </div>
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {:else}
        <div class="max-h-[70vh] overflow-y-auto">
          {#if snapshot.false_positives.length === 0}
            <p class="text-gray-500">No false positives</p>
          {:else}
            <div class="space-y-2">
              {#each snapshot.false_positives as fp (fp.fp_id)}
                <div class="border rounded">
                  <button
                    class="w-full px-3 py-2 flex justify-between items-center hover:bg-gray-50 text-left"
                    onclick={() => expandedIssues.toggle(fp.fp_id)}
                  >
                    <div class="flex items-center gap-2">
                      <span class="text-gray-400">{expandedIssues.isExpanded(fp.fp_id) ? "▼" : "▶"}</span>
                      <span class="font-mono text-sm font-medium">{fp.fp_id}</span>
                      <span class="text-xs text-gray-500">({fp.occurrences.length} occ)</span>
                    </div>
                  </button>

                  {#if expandedIssues.isExpanded(fp.fp_id)}
                    <div class="px-3 pb-3 border-t bg-gray-50">
                      <div class="mt-2">
                        <h4 class="text-xs font-medium text-gray-500 uppercase mb-1">Rationale</h4>
                        <p class="text-sm whitespace-pre-wrap">{fp.rationale}</p>
                      </div>
                      <div class="mt-3">
                        <h4 class="text-xs font-medium text-gray-500 uppercase mb-1">Occurrences</h4>
                        {#each fp.occurrences as occ (occ.occurrence_id)}
                          <div
                            id="{fp.fp_id}-{occ.occurrence_id}"
                            class="bg-white border rounded p-2 mt-1 {parsedSlug.occurrenceId === occ.occurrence_id
                              ? 'ring-2 ring-blue-500'
                              : ''}"
                          >
                            <div class="flex items-center justify-between">
                              <div class="text-xs">
                                <OccurrenceLink
                                  snapshotSlug={snapshot.slug}
                                  issueId={fp.fp_id}
                                  occurrenceId={occ.occurrence_id}
                                  filePath={occ.files[0]?.path}
                                />
                              </div>
                              <CopyButton
                                text={getOccurrenceUrl(fp.fp_id, occ.occurrence_id, occ.files[0]?.path)}
                                label="Copy URL"
                              />
                            </div>
                            <div class="mt-1">
                              {#each occ.files as file (`${occ.occurrence_id}-${file.path}`)}
                                <div class="text-sm font-mono">{formatFileLocation(file)}</div>
                              {/each}
                            </div>
                            {#if occ.note}
                              <div class="mt-1 text-sm text-gray-600 italic">{occ.note}</div>
                            {/if}
                            {#if occ.relevant_files && occ.relevant_files.length > 0}
                              <div class="mt-1 text-xs text-gray-500">
                                Relevant: {occ.relevant_files.join(", ")}
                              </div>
                            {/if}
                          </div>
                        {/each}
                      </div>
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/if}
    </div>
  </div>
{/if}
