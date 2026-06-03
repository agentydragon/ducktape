<script lang="ts">
  import "./app.css";
  import { onMount, setContext } from "svelte";
  import { Toaster } from "svelte-sonner";
  import { pathname, resolve, parseParams } from "$lib/router";
  import { captureTokenFromUrl, getToken, setToken, clearToken, needsToken } from "$lib/stores/token";
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
  // While true, we're still determining auth (probing for an SSO session cookie),
  // so render neither the login screen nor the app to avoid a flash.
  let checking = $state(true);
  let userEmail = $state<string | null>(null);
  let showTokenLogin = $state(false);
  // Disable the other mode when one has input
  const tokenHasInput = $derived(tokenInput.trim().length > 0);
  const credsHasInput = $derived(usernameInput.trim().length > 0 || passwordInput.length > 0);

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
  }

  async function handleLogout() {
    clearToken();
    // Clear the server-side SSO session too (no-op/404 when SSO is disabled).
    await fetch("/auth/logout", { credentials: "include" }).catch(() => undefined);
    window.location.href = "/";
  }

  async function initAuth() {
    captureTokenFromUrl();
    if (getToken()) {
      needsToken.set(false);
      return;
    }
    // No stored token: check for a valid Authentik SSO session cookie.
    try {
      const resp = await fetch("/auth/me", { credentials: "include" });
      if (resp.ok) {
        const data = (await resp.json()) as { email: string };
        userEmail = data.email;
        needsToken.set(false);
      } else {
        needsToken.set(true);
      }
    } catch {
      needsToken.set(true);
    }
  }

  onMount(() => {
    initAuth().finally(() => {
      checking = false;
    });
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

{#if checking}
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center">
    <div class="text-sm text-gray-500 dark:text-gray-400">Loading…</div>
  </div>
{:else if $needsToken}
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center">
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow-md dark:shadow-gray-950/50 p-8 max-w-md w-full">
      <h1 class="text-xl font-bold mb-4">Props</h1>
      <a
        href="/auth/login"
        class="block w-full text-center px-4 py-2 bg-blue-600 text-white rounded text-sm font-medium hover:bg-blue-700"
      >
        Sign in with Authentik
      </a>
      {#if showTokenLogin}
        <div class="flex items-center gap-2 my-4">
          <div class="flex-1 border-t border-gray-200 dark:border-gray-700"></div>
          <span class="text-xs text-gray-400 dark:text-gray-500">or use a token</span>
          <div class="flex-1 border-t border-gray-200 dark:border-gray-700"></div>
        </div>
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
              class="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded text-sm bg-white dark:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 dark:disabled:bg-gray-800 disabled:cursor-not-allowed"
            />
            <input
              type="password"
              bind:value={passwordInput}
              placeholder="Password"
              autocomplete="current-password"
              disabled={tokenHasInput}
              class="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded text-sm bg-white dark:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 dark:disabled:bg-gray-800 disabled:cursor-not-allowed"
            />
          </div>
          <div class="flex items-center gap-2">
            <div class="flex-1 border-t border-gray-200 dark:border-gray-700"></div>
            <span class="text-xs text-gray-400 dark:text-gray-500">or token</span>
            <div class="flex-1 border-t border-gray-200 dark:border-gray-700"></div>
          </div>
          <div class="transition-opacity {credsHasInput ? 'opacity-40' : ''}">
            <input
              type="text"
              bind:value={tokenInput}
              placeholder="base64 token"
              disabled={credsHasInput}
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded text-sm bg-white dark:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 dark:disabled:bg-gray-800 disabled:cursor-not-allowed"
            />
          </div>
          <button type="submit" class="px-4 py-2 bg-blue-600 text-white rounded text-sm font-medium hover:bg-blue-700">
            Sign in
          </button>
        </form>
      {:else}
        <button
          type="button"
          onclick={() => (showTokenLogin = true)}
          class="mt-3 block w-full text-center text-xs text-gray-500 dark:text-gray-400 hover:underline"
        >
          Use a token instead
        </button>
      {/if}
    </div>
  </div>
{:else}
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900">
    <!-- Header -->
    <header class="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-6 py-3">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-3">
          <h1 class="text-xl font-bold">
            <a href={resolve("/")} class="hover:text-blue-600">Props</a>
          </h1>
        </div>
        <nav class="flex gap-1">
          {#each navItems as { path, label } (path)}
            <a
              href={resolve(path)}
              class="px-3 py-1.5 rounded text-sm font-medium transition-colors
                {isActive(path, $pathname)
                ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300'
                : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-gray-100'}"
            >
              {label}
            </a>
          {/each}
        </nav>
        <div class="flex items-center gap-3">
          {#if userEmail}
            <span class="text-xs text-gray-500 dark:text-gray-400">{userEmail}</span>
          {/if}
          <button
            type="button"
            onclick={handleLogout}
            class="px-3 py-1.5 rounded text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
          >
            Logout
          </button>
        </div>
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
          <h2 class="text-2xl font-bold text-gray-900 dark:text-gray-100">Page Not Found</h2>
          <p class="mt-2 text-gray-600 dark:text-gray-400">The page you're looking for doesn't exist.</p>
          <a href={resolve("/")} class="mt-4 inline-block text-blue-600 dark:text-blue-400 hover:underline">Go home</a>
        </div>
      {/if}
    </main>
  </div>

  <RunTriggerModal open={showRunModal} onClose={handleCloseRunModal} prefill={modalPrefill} />
{/if}
