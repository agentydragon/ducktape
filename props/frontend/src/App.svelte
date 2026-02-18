<script lang="ts">
  import "./app.css";
  import { onMount, setContext } from "svelte";
  import { Toaster } from "svelte-sonner";
  import { pathname, resolve, goto, parseParams } from "$lib/router";
  import { connected, startFeed } from "$lib/stores/runsFeed";
  import { captureTokenFromUrl, getToken, setToken, needsToken } from "$lib/stores/token";
  import RunTriggerModal from "$components/RunTriggerModal.svelte";
  import type { Split, ExampleKind } from "$lib/types";

  // Page components
  import OverviewPage from "./pages/OverviewPage.svelte";
  import RunsPage from "./pages/RunsPage.svelte";
  import RunDetailPage from "./pages/RunDetailPage.svelte";
  import DefinitionDetailPage from "./pages/DefinitionDetailPage.svelte";
  import ExamplesPage from "./pages/ExamplesPage.svelte";
  import SnapshotsPage from "./pages/SnapshotsPage.svelte";
  import SnapshotDetailPage from "./pages/SnapshotDetailPage.svelte";

  interface ModalPrefill {
    definitionId?: string;
    split?: Split;
    kind?: ExampleKind;
  }

  let showRunModal = $state(false);
  let modalPrefill: ModalPrefill | undefined = $state(undefined);
  let tokenInput = $state("");
  let usernameInput = $state("");
  let passwordInput = $state("");

  // Disable the other mode when one has input
  let tokenHasInput = $derived(tokenInput.trim().length > 0);
  let credsHasInput = $derived(usernameInput.trim().length > 0 || passwordInput.length > 0);

  function handleOpenRunModal(prefill?: ModalPrefill) {
    modalPrefill = prefill;
    showRunModal = true;
  }

  function handleCloseRunModal() {
    showRunModal = false;
    modalPrefill = undefined;
  }

  // Expose modal functions to child components
  setContext("runModal", { open: handleOpenRunModal });

  function handleLogin() {
    const token = tokenInput.trim();
    const user = usernameInput.trim();
    const pass = passwordInput;

    if (token) {
      setToken(token);
      tokenInput = "";
    } else if (user && pass) {
      setToken(btoa(`${user}:${pass}`));
      usernameInput = "";
      passwordInput = "";
    } else {
      return;
    }
    startFeed();
  }

  onMount(() => {
    captureTokenFromUrl();
    if (getToken()) {
      startFeed();
    } else {
      needsToken.set(true);
    }
  });

  // Navigation items
  const navItems = [
    { path: "/", label: "Overview" },
    { path: "/runs", label: "Runs" },
    { path: "/snapshots", label: "Ground Truth" },
  ];

  function isActive(path: string, currentPath: string): boolean {
    if (path === "/") return currentPath === "/";
    return currentPath.startsWith(path);
  }

  // Route matching — $pathname is already the clean path (no query string).
  const currentRoute = $derived.by(() => {
    const path = $pathname;

    // Match routes in order of specificity
    let params: Record<string, string> | null;

    // /runs/:runId
    params = parseParams("/runs/[runId]", path);
    if (params) return { component: "run-detail", params };

    // /runs
    if (path === "/runs") return { component: "runs", params: {} };

    // /definitions/:definitionId
    params = parseParams("/definitions/[definitionId]", path);
    if (params) return { component: "definition-detail", params };

    // /examples
    if (path === "/examples") return { component: "examples", params: {} };

    // /snapshots/:slug (catch-all for nested paths)
    params = parseParams("/snapshots/[...slug]", path);
    if (params) return { component: "snapshot-detail", params };

    // /snapshots
    if (path === "/snapshots") return { component: "snapshots", params: {} };

    // / (home)
    if (path === "/" || path === "") return { component: "overview", params: {} };

    // 404
    return { component: "not-found", params: {} };
  });
</script>

<Toaster richColors position="top-right" duration={8000} />

{#if $needsToken}
  <div class="min-h-screen bg-gray-50 flex items-center justify-center">
    <div class="bg-white rounded-lg shadow-md p-8 max-w-md w-full">
      <h1 class="text-xl font-bold mb-2">Props</h1>
      <form
        onsubmit={(e: SubmitEvent) => {
          e.preventDefault();
          handleLogin();
        }}
        class="flex flex-col gap-3"
      >
        <div class="flex flex-col gap-2 transition-opacity {tokenHasInput ? 'opacity-40' : ''}">
          <input
            type="text"
            bind:value={usernameInput}
            placeholder="Username"
            autocomplete="username"
            disabled={tokenHasInput}
            class="px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 disabled:cursor-not-allowed"
          />
          <input
            type="password"
            bind:value={passwordInput}
            placeholder="Password"
            autocomplete="current-password"
            disabled={tokenHasInput}
            class="px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 disabled:cursor-not-allowed"
          />
        </div>
        <div class="flex items-center gap-2">
          <div class="flex-1 border-t border-gray-200"></div>
          <span class="text-xs text-gray-400">or token</span>
          <div class="flex-1 border-t border-gray-200"></div>
        </div>
        <div class="transition-opacity {credsHasInput ? 'opacity-40' : ''}">
          <input
            type="text"
            bind:value={tokenInput}
            placeholder="base64 token"
            disabled={credsHasInput}
            class="w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 disabled:cursor-not-allowed"
          />
        </div>
        <button type="submit" class="px-4 py-2 bg-blue-600 text-white rounded text-sm font-medium hover:bg-blue-700">
          Sign in
        </button>
      </form>
    </div>
  </div>
{:else}
  <div class="min-h-screen bg-gray-50">
    <!-- Header -->
    <header class="bg-white border-b border-gray-200 px-6 py-3">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-3">
          <h1 class="text-xl font-bold">
            <a href={resolve("/")} class="hover:text-blue-600">Props</a>
          </h1>
          {#if $connected}
            <span class="px-2 py-0.5 text-xs bg-green-100 text-green-700 rounded">live</span>
          {:else}
            <span class="px-2 py-0.5 text-xs bg-orange-100 text-orange-700 rounded">reconnecting...</span>
          {/if}
        </div>
        <nav class="flex gap-1">
          {#each navItems as { path, label } (path)}
            <a
              href={resolve(path)}
              class="px-3 py-1.5 rounded text-sm font-medium transition-colors
                {isActive(path, $pathname)
                ? 'bg-blue-100 text-blue-700'
                : 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'}"
            >
              {label}
            </a>
          {/each}
        </nav>
      </div>
    </header>

    <!-- Main content -->
    <main class="p-6">
      {#if currentRoute.component === "overview"}
        <OverviewPage />
      {:else if currentRoute.component === "runs"}
        <RunsPage />
      {:else if currentRoute.component === "run-detail"}
        <RunDetailPage runId={currentRoute.params.runId} />
      {:else if currentRoute.component === "definition-detail"}
        <DefinitionDetailPage definitionId={currentRoute.params.definitionId} />
      {:else if currentRoute.component === "examples"}
        <ExamplesPage />
      {:else if currentRoute.component === "snapshots"}
        <SnapshotsPage />
      {:else if currentRoute.component === "snapshot-detail"}
        <SnapshotDetailPage slug={currentRoute.params.slug} />
      {:else}
        <div class="text-center py-12">
          <h2 class="text-2xl font-bold text-gray-900">Page Not Found</h2>
          <p class="mt-2 text-gray-600">The page you're looking for doesn't exist.</p>
          <a href={resolve("/")} class="mt-4 inline-block text-blue-600 hover:underline">Go home</a>
        </div>
      {/if}
    </main>
  </div>

  <RunTriggerModal open={showRunModal} onClose={handleCloseRunModal} prefill={modalPrefill} />
{/if}
