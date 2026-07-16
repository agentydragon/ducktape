<script lang="ts">
  import { onMount } from "svelte";
  import { getApiClient } from "./api.ts";
  import OAuthProviders from "./OAuthProviders.svelte";
  import type { DeploymentInfo } from "./types.ts";

  let deploymentInfo = $state<DeploymentInfo | null>(null);

  onMount(async () => {
    try {
      deploymentInfo = await getApiClient().getDeploymentInfo();
    } catch (error) {
      console.error("Failed to load Airlock deployment info", error);
    }
  });
</script>

<header class="app-header px-4 py-3 sm:px-6 flex items-baseline gap-3">
  <h1 class="app-header-title text-lg font-semibold m-0">Airlock</h1>
  <span class="app-header-subtitle text-sm">OAuth credential broker</span>
</header>

<main class="max-w-4xl mx-auto px-4 py-6">
  <OAuthProviders />
</main>

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
