<script lang="ts">
  import { getApiClient } from "./api.ts";
  import { getAccessToken } from "./auth.ts";
  import type { OAuthProviderStatus } from "./types.ts";
  import { onMount } from "svelte";

  let providers = $state<OAuthProviderStatus[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let plaidLoading = $state<string | null>(null);

  onMount(async () => {
    try {
      const api = await getApiClient();
      providers = await api.listOAuthProviders();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  function fmtExpiry(iso: string | null): string {
    if (!iso) return "N/A";
    return new Date(iso).toLocaleString();
  }

  async function connectPlaid(providerName: string): Promise<void> {
    plaidLoading = providerName;
    try {
      const token = await getAccessToken();
      const resp = await fetch(`/oauth/authorize/${providerName}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok) throw new Error(`Failed to get link token: ${resp.status}`);
      const data = await resp.json();

      // Load Plaid Link SDK if not already loaded
      if (!(window as any).Plaid) {
        await new Promise<void>((resolve, reject) => {
          const script = document.createElement("script");
          script.src = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";
          script.onload = () => resolve();
          script.onerror = () => reject(new Error("Failed to load Plaid SDK"));
          document.head.appendChild(script);
        });
      }

      const handler = (window as any).Plaid.create({
        token: data.link_token,
        receivedRedirectUri: data.received_redirect_uri ?? undefined,
        onSuccess: async (publicToken: string) => {
          await fetch(`/oauth/callback/${providerName}`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
            body: JSON.stringify({ public_token: publicToken }),
          });
          // Refresh provider list
          const api = await getApiClient();
          providers = await api.listOAuthProviders();
        },
        onExit: (err: any) => {
          if (err) error = `Plaid Link error: ${JSON.stringify(err)}`;
        },
      });
      handler.open();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      plaidLoading = null;
    }
  }
</script>

<h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-3">OAuth Providers</h2>

{#if loading}
  <p class="section-heading italic py-4">Loading providers…</p>
{:else if error}
  <p class="font-medium" style="color: var(--color-error);">Failed to load providers: {error}</p>
{:else if providers.length === 0}
  <p class="section-heading italic py-4">No OAuth providers configured.</p>
{:else}
  <div class="space-y-4">
    {#each providers as provider (provider.name)}
      <div class="card rounded-lg shadow-sm p-5">
        <div class="flex items-start justify-between gap-4">
          <div class="flex-1">
            <h3 class="text-sm font-semibold m-0" style="color: var(--color-text);">{provider.display_name}</h3>
            <dl class="grid grid-cols-1 sm:grid-cols-[120px_1fr] gap-x-4 gap-y-1 text-sm mt-3">
              <dt class="section-heading font-semibold">Type</dt>
              <dd class="m-0"><code class="code-tag text-xs rounded px-1.5 py-0.5">{provider.provider_type}</code></dd>
              <dt class="section-heading font-semibold">Status</dt>
              <dd class="m-0">
                {#if provider.connected}
                  <span class="status-pill status-pill-done">Connected</span>
                {:else}
                  <span class="status-pill status-pill-pending">Not connected</span>
                {/if}
              </dd>
              {#if provider.connected}
                <dt class="section-heading font-semibold">Expires</dt>
                <dd class="m-0" style="color: var(--color-text-muted);">{fmtExpiry(provider.expires_at)}</dd>
              {/if}
              {#if provider.scope}
                <dt class="section-heading font-semibold">Scopes</dt>
                <dd class="m-0" style="color: var(--color-text-muted);">{provider.scope}</dd>
              {/if}
            </dl>
          </div>
          {#if provider.provider_type === "plaid"}
            <button
              onclick={() => connectPlaid(provider.name)}
              disabled={plaidLoading === provider.name}
              class="btn-approve font-semibold px-5 py-2.5 rounded-lg border-0 cursor-pointer transition-colors text-sm"
            >
              {plaidLoading === provider.name ? "Loading…" : provider.connected ? "Reconnect" : "Connect"}
            </button>
          {:else}
            <a
              href="/oauth/authorize/{provider.name}"
              class="btn-approve font-semibold px-5 py-2.5 rounded-lg border-0 cursor-pointer transition-colors text-sm no-underline"
            >
              {provider.connected ? "Reconnect" : "Connect"}
            </a>
          {/if}
        </div>
      </div>
    {/each}
  </div>
{/if}
