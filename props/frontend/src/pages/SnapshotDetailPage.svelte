<script lang="ts">
  import { searchParams, resolve } from "$lib/router";
  import { toast } from "svelte-sonner";
  import { splitBadgeClass } from "$lib/colors";
  import { formatLocationAnchor } from "$lib/formatters";
  import type { SnapshotDetailResponse, FileContentResponse, FileTreeResponse, ClusterResponse } from "$lib/api/client";
  import { fetchSnapshotDetail, fetchSnapshotTree, fetchSnapshotFile, fetchSnapshotClusters } from "$lib/api/client";
  import FileTree from "$components/FileTree.svelte";
  import FileViewer from "$components/FileViewer.svelte";
  import TabButton from "$components/TabButton.svelte";
  import Breadcrumb from "$components/Breadcrumb.svelte";
  import CopyButton from "$components/CopyButton.svelte";
  import BackButton from "$components/BackButton.svelte";
  import OccurrenceLink from "$lib/OccurrenceLink.svelte";
  import CreditBadge from "$components/stats/CreditBadge.svelte";
  import OccurrenceStats from "$components/stats/OccurrenceStats.svelte";
  import { fetchOccurrenceStats } from "$lib/api/client";
  import type { OccurrenceStatsRow } from "$lib/api/client";
  import { createExpansionState } from "$lib/expansionState.svelte";

  interface Props {
    slug: string; // This is the full catch-all path: "snapshot-name" or "snapshot-name/issueId/occurrenceId"
    initialSnapshot?: SnapshotDetailResponse;
    initialTree?: FileTreeResponse;
  }
  let { slug, initialSnapshot, initialTree }: Props = $props();

  // Parse the slug into components.
  // Snapshot slugs are "org/date" (2 parts), optionally followed by issueId/occurrenceId.
  const parsedSlug = $derived.by(() => {
    const parts = slug.split("/");
    const snapshotSlug = parts.slice(0, 2).join("/");
    const issueId = parts.length >= 4 ? parts[2] : undefined;
    const occurrenceId = parts.length >= 4 ? parts[3] : undefined;
    return { snapshotSlug, issueId, occurrenceId };
  });

  const targetFile = $derived($searchParams.get("file") || undefined);

  // svelte-ignore state_referenced_locally
  let snapshot: SnapshotDetailResponse | null = $state(initialSnapshot ?? null);
  // svelte-ignore state_referenced_locally
  let tree: FileTreeResponse | null = $state(initialTree ?? null);
  // svelte-ignore state_referenced_locally
  let loading = $state(!initialSnapshot);
  let error: string | null = $state(null);

  const expandedIssues = createExpansionState();
  let activeTab: "files" | "tps" | "fps" | "clusters" | "stats" = $state("files");
  let selectedFile: FileContentResponse | null = $state(null);
  let loadingFile = $state(false);
  let occurrenceStats: OccurrenceStatsRow[] = $state([]);
  let occurrenceStatsMap = $derived(new Map(occurrenceStats.map((o) => [`${o.tp_id}:${o.occurrence_id}`, o])));
  let clusters: ClusterResponse[] = $state([]);

  async function loadData() {
    loading = true;
    error = null;
    // Reset stale data from previous snapshot
    occurrenceStats = [];
    clusters = [];
    selectedFile = null;
    try {
      const [snapshotData, treeData] = await Promise.all([
        fetchSnapshotDetail(parsedSlug.snapshotSlug),
        fetchSnapshotTree(parsedSlug.snapshotSlug),
      ]);
      snapshot = snapshotData;
      tree = treeData;
      // Fetch occurrence stats and clusters (non-blocking)
      // Capture slug to guard against stale responses if the user navigates away
      const currentSlug = parsedSlug.snapshotSlug;
      fetchOccurrenceStats(currentSlug).then(
        (data) => {
          if (parsedSlug.snapshotSlug === currentSlug) {
            occurrenceStats = data.occurrences;
          }
        },
        (e) => {
          if (parsedSlug.snapshotSlug === currentSlug) {
            toast.error(e instanceof Error ? e.message : "Failed to load occurrence stats");
          }
        }
      );
      fetchSnapshotClusters(currentSlug).then(
        (data) => {
          if (parsedSlug.snapshotSlug === currentSlug) {
            clusters = data.clusters;
          }
        },
        (e) => {
          if (parsedSlug.snapshotSlug === currentSlug) {
            toast.error(e instanceof Error ? e.message : "Failed to load clusters");
          }
        }
      );
    } catch (e) {
      error = e instanceof Error ? e.message : "Failed to load snapshot";
    } finally {
      loading = false;
    }
  }

  // $effect runs immediately on mount and re-runs when snapshotSlug changes
  $effect(() => {
    if (parsedSlug.snapshotSlug && !initialSnapshot) {
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
      return `${window.location.origin}${hashPath}?file=${encodeURIComponent(filePath)}`;
    }
    return `${window.location.origin}${hashPath}`;
  }

  let pendingScrollTarget: string | null = $state(null);

  function findAndNavigateToOccurrence(issueId: string, occurrenceId: string, filePath?: string) {
    if (!snapshot) return;
    const snap = snapshot; // Capture for closure

    const searchInIssues = (issues: typeof snap.true_positives | typeof snap.false_positives) => {
      for (const issue of issues) {
        const issueIdMatch = "tp_id" in issue ? issue.tp_id === issueId : issue.fp_id === issueId;
        if (issueIdMatch) {
          const occ = issue.occurrences.find((o) => o.occurrence_id === occurrenceId);
          if (occ?.locations.length) {
            // Use specified file or first file
            const fileToLoad = filePath || occ.locations[0].file;
            handleFileClick(fileToLoad);
            expandedIssues.expand(issueId);
            activeTab = "files";
            // Defer scroll until file finishes loading and renders
            pendingScrollTarget = `${issueId}-${occurrenceId}`;
            return true;
          }
        }
      }
      return false;
    };

    searchInIssues(snap.true_positives) || searchInIssues(snap.false_positives);
  }

  // Scroll to target after file finishes loading
  $effect(() => {
    if (pendingScrollTarget && selectedFile && !loadingFile) {
      const target = pendingScrollTarget;
      pendingScrollTarget = null;
      // Use tick to wait for DOM update after state change
      requestAnimationFrame(() => {
        document.getElementById(target)?.scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
      });
    }
  });

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
        <TabButton active={activeTab === "clusters"} onclick={() => (activeTab = "clusters")}>
          Clusters ({clusters.length})
        </TabButton>
        <TabButton active={activeTab === "stats"} onclick={() => (activeTab = "stats")}>Detection Stats</TabButton>
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
                            class="bg-white border rounded p-2 mt-1 {parsedSlug.issueId === tp.tp_id &&
                            parsedSlug.occurrenceId === occ.occurrence_id
                              ? 'ring-2 ring-blue-500'
                              : ''}"
                          >
                            <div class="flex items-center justify-between">
                              <div class="flex items-center gap-2 text-xs">
                                <OccurrenceLink
                                  snapshotSlug={snapshot.slug}
                                  issueId={tp.tp_id}
                                  occurrenceId={occ.occurrence_id}
                                  filePath={occ.locations[0]?.file}
                                />
                                {#if occurrenceStatsMap.has(`${tp.tp_id}:${occ.occurrence_id}`)}
                                  {@const stats = occurrenceStatsMap.get(`${tp.tp_id}:${occ.occurrence_id}`)!}
                                  <CreditBadge meanCredit={stats.mean_credit} nRuns={stats.n_runs} />
                                {/if}
                              </div>
                              <CopyButton
                                text={getOccurrenceUrl(tp.tp_id, occ.occurrence_id, occ.locations[0]?.file)}
                                label="Copy URL"
                              />
                            </div>
                            <div class="mt-1">
                              {#each occ.locations as loc (`${occ.occurrence_id}-${loc.file}`)}
                                <div class="text-sm font-mono">
                                  {formatLocationAnchor(loc)}
                                  {#if loc.note}
                                    <span class="italic text-gray-500 ml-1">({loc.note})</span>
                                  {/if}
                                </div>
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
      {:else if activeTab === "fps"}
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
                            class="bg-white border rounded p-2 mt-1 {parsedSlug.issueId === fp.fp_id &&
                            parsedSlug.occurrenceId === occ.occurrence_id
                              ? 'ring-2 ring-blue-500'
                              : ''}"
                          >
                            <div class="flex items-center justify-between">
                              <div class="text-xs">
                                <OccurrenceLink
                                  snapshotSlug={snapshot.slug}
                                  issueId={fp.fp_id}
                                  occurrenceId={occ.occurrence_id}
                                  filePath={occ.locations[0]?.file}
                                />
                              </div>
                              <CopyButton
                                text={getOccurrenceUrl(fp.fp_id, occ.occurrence_id, occ.locations[0]?.file)}
                                label="Copy URL"
                              />
                            </div>
                            <div class="mt-1">
                              {#each occ.locations as loc (`${occ.occurrence_id}-${loc.file}`)}
                                <div class="text-sm font-mono">
                                  {formatLocationAnchor(loc)}
                                  {#if loc.note}
                                    <span class="italic text-gray-500 ml-1">({loc.note})</span>
                                  {/if}
                                </div>
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
      {:else if activeTab === "clusters"}
        <div class="max-h-[70vh] overflow-y-auto">
          {#if clusters.length === 0}
            <p class="text-gray-500">No clusters</p>
          {:else}
            <div class="space-y-2">
              {#each clusters as cluster (cluster.cluster_id)}
                <div class="border rounded">
                  <button
                    class="w-full px-3 py-2 flex justify-between items-center hover:bg-gray-50 text-left"
                    onclick={() => expandedIssues.toggle(`cluster-${cluster.cluster_id}`)}
                  >
                    <div class="flex items-center gap-2">
                      <span class="text-gray-400"
                        >{expandedIssues.isExpanded(`cluster-${cluster.cluster_id}`) ? "▼" : "▶"}</span
                      >
                      <span class="font-mono text-sm font-medium">{cluster.cluster_id}</span>
                      <span class="text-xs text-gray-500">({cluster.members.length} issues)</span>
                    </div>
                  </button>

                  {#if expandedIssues.isExpanded(`cluster-${cluster.cluster_id}`)}
                    <div class="px-3 pb-3 border-t bg-gray-50">
                      <div class="mt-2">
                        <h4 class="text-xs font-medium text-gray-500 uppercase mb-1">Description</h4>
                        <p class="text-sm whitespace-pre-wrap">{cluster.rationale}</p>
                      </div>
                      <div class="mt-3">
                        <h4 class="text-xs font-medium text-gray-500 uppercase mb-1">Member Issues</h4>
                        {#each cluster.members as member (`${member.critique_run_id}-${member.critique_issue_id}`)}
                          <div class="bg-white border rounded p-2 mt-1">
                            <div class="flex items-center gap-2 text-xs">
                              <a
                                href={resolve(`/runs/${member.critique_run_id}`)}
                                class="font-mono text-blue-600 hover:underline"
                              >
                                {member.critique_run_id.slice(0, 8)}
                              </a>
                              <span class="text-gray-400">/</span>
                              <span class="font-mono font-medium">{member.critique_issue_id}</span>
                            </div>
                            {#if member.issue_rationale}
                              <div class="mt-1 text-sm text-gray-700">{member.issue_rationale}</div>
                            {/if}
                            {#if member.rationale}
                              <div class="mt-1 text-xs text-gray-500 italic">{member.rationale}</div>
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
      {:else if activeTab === "stats"}
        <div class="max-h-[70vh] overflow-y-auto">
          <OccurrenceStats occurrences={occurrenceStats} />
        </div>
      {/if}
    </div>
  </div>
{/if}
