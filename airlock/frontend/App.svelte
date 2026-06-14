<script lang="ts">
  import { onMount } from "svelte";
  import { getApiClient } from "./api.ts";
  import ActionList from "./ActionList.svelte";
  import ActionDetail from "./ActionDetail.svelte";
  import BackendStatus from "./BackendStatus.svelte";
  import OAuthProviders from "./OAuthProviders.svelte";
  import type { Action, DeploymentInfo } from "./types.ts";

  type Route =
    | { kind: "list" }
    | { kind: "action"; sessionKey: string; actionSeq: number }
    | { kind: "oauth" }
    | { kind: "backends" };

  function parseRoute(): Route {
    const hash = window.location.hash;
    const actionMatch = hash.match(/^#\/sessions\/([^/]+)\/actions\/(\d+)\/?$/);
    if (actionMatch) {
      return { kind: "action", sessionKey: actionMatch[1], actionSeq: parseInt(actionMatch[2], 10) };
    }
    if (hash === "#/oauth") {
      return { kind: "oauth" };
    }
    if (hash === "#/backends") {
      return { kind: "backends" };
    }
    return { kind: "list" };
  }

  let route = $state<Route>(parseRoute());

  let pending = $state<Action[]>([]);
  let recent = $state<Action[]>([]);
  let action = $state<Action | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let deploymentInfo = $state<DeploymentInfo | null>(null);

  async function loadList(): Promise<void> {
    const api = await getApiClient();
    [pending, recent] = await Promise.all([api.listActions("pending"), api.listActions(undefined, 20)]);
  }

  onMount(() => {
    // Re-parse the route on every hash navigation so menu clicks update the page,
    // not just the URL.
    const onHashChange = () => {
      route = parseRoute();
    };
    window.addEventListener("hashchange", onHashChange);

    // Best-effort deployment info — route-independent, loaded once for the footer.
    (async () => {
      try {
        const api = await getApiClient();
        deploymentInfo = await api.getDeploymentInfo();
      } catch {
        // ignore — footer is hidden if we can't get it
      }
    })();

    return () => window.removeEventListener("hashchange", onHashChange);
  });

  // Load route-specific data whenever the route changes. The cleanup tears down
  // the previous route's subscription so stale callbacks don't mutate state.
  $effect(() => {
    const current = route;
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;

    loading = true;
    error = null;
    action = null;

    (async () => {
      const api = await getApiClient();
      if (cancelled) return;
      if (current.kind === "action") {
        try {
          unsubscribe = await api.subscribeAction<Action>(current.sessionKey, current.actionSeq, (a) => {
            action = a;
            loading = false;
            error = null;
          });
          if (loading && !cancelled) {
            error = "Failed to read action resource";
            loading = false;
          }
        } catch (err) {
          if (!cancelled) {
            error = String(err);
            loading = false;
          }
        }
      } else if (current.kind === "list") {
        try {
          await loadList();
        } catch (e) {
          if (!cancelled) error = String(e);
        } finally {
          if (!cancelled) loading = false;
        }
        unsubscribe = api.onListChanged(() => loadList());
      } else {
        loading = false;
      }
    })();

    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  });
</script>

<header class="app-header px-4 py-3 sm:px-6 flex items-center gap-3">
  {#if route.kind === "action" && action}
    <h1 class="text-lg font-semibold m-0" style="color: var(--color-header-text);">
      <a href="#/" class="app-header-link">Airlock</a>
      <span class="font-normal" style="color: var(--color-header-text-dim);"> / </span>
      <span class="text-sm font-normal" style="color: var(--color-header-link);">
        Action {action.key.session_key}/{action.key.action_seq}
      </span>
    </h1>
  {:else}
    <h1 class="app-header-title text-lg font-semibold m-0">Airlock</h1>
    {#if route.kind === "list" && pending.length > 0}
      <span class="badge text-xs font-bold rounded-full px-2.5 py-0.5">{pending.length} pending</span>
    {/if}
  {/if}
  <nav class="flex gap-3 ml-auto text-sm">
    <a href="#/" class="app-header-link">Actions</a>
    <a href="#/backends" class="app-header-link">Backends</a>
    <a href="#/oauth" class="app-header-link">OAuth</a>
  </nav>
</header>

{#if loading}
  <main class="max-w-4xl mx-auto px-4 py-6"><p class="section-heading">Loading…</p></main>
{:else if error}
  <main class="max-w-4xl mx-auto px-4 py-6">
    <p class="font-medium" style="color: var(--color-error);">Failed to load: {error}</p>
  </main>
{:else if route.kind === "action"}
  {#if action}
    <ActionDetail {action} />
  {:else}
    <main class="max-w-4xl mx-auto px-4 py-6">
      <p class="font-medium" style="color: var(--color-error);">Action not found.</p>
    </main>
  {/if}
{:else if route.kind === "backends"}
  <main class="max-w-4xl mx-auto px-4 py-6">
    <BackendStatus />
  </main>
{:else if route.kind === "oauth"}
  <main class="max-w-4xl mx-auto px-4 py-6">
    <OAuthProviders />
  </main>
{:else}
  <ActionList {pending} {recent} />
{/if}

{#if deploymentInfo?.image_tag || deploymentInfo?.source_commit}
  <footer class="app-footer max-w-4xl mx-auto px-4 py-4 text-xs flex flex-wrap justify-center gap-2">
    <span style="color: var(--color-text-muted);">Deployed commit</span>
    {#if deploymentInfo.source_commit_url}
      <a
        href={deploymentInfo.source_commit_url}
        target="_blank"
        rel="noreferrer"
        class="font-mono"
        style="color: var(--color-link);"
      >
        {deploymentInfo.source_commit?.slice(0, 7) ?? "unknown"}
      </a>
    {:else}
      <span class="font-mono" style="color: var(--color-text-muted);">
        {deploymentInfo.source_commit?.slice(0, 7) ?? "unknown"}
      </span>
    {/if}
    {#if deploymentInfo.image_tag}
      <span class="font-mono" title={deploymentInfo.image_tag} style="color: var(--color-text-muted); opacity: 0.65;">
        {deploymentInfo.image_tag}
      </span>
    {/if}
  </footer>
{/if}
