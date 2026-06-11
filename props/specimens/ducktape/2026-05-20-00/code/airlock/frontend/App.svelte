<script lang="ts">
  import { onMount } from "svelte";
  import { getApiClient } from "./api.ts";
  import ActionList from "./ActionList.svelte";
  import ActionDetail from "./ActionDetail.svelte";
  import BackendStatus from "./BackendStatus.svelte";
  import OAuthProviders from "./OAuthProviders.svelte";
  import type { Action } from "./types.ts";

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

  const route = parseRoute();

  let pending = $state<Action[]>([]);
  let recent = $state<Action[]>([]);
  let action = $state<Action | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);

  async function loadList(): Promise<void> {
    const api = await getApiClient();
    [pending, recent] = await Promise.all([api.listActions("pending"), api.listActions(undefined, 20)]);
  }

  onMount(async () => {
    if (route.kind === "action") {
      try {
        const api = await getApiClient();
        await api.subscribeAction<Action>(route.sessionKey, route.actionSeq, (a) => {
          action = a;
          loading = false;
          error = null;
        });
        if (loading) {
          error = "Failed to read action resource";
          loading = false;
        }
      } catch (err) {
        error = String(err);
        loading = false;
      }
    } else if (route.kind === "list") {
      try {
        await loadList();
      } catch (e) {
        error = String(e);
      } finally {
        loading = false;
      }
      const api = await getApiClient();
      api.onListChanged(() => {
        loadList();
      });
    } else {
      loading = false;
    }
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
