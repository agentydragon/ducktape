<script lang="ts">
  import type { Action } from "./types.ts";

  let { pending, recent }: { pending: Action[]; recent: Action[] } = $props();

  function fmt(iso: string): string {
    return new Date(iso).toLocaleString();
  }

  function actionHref(a: Action): string {
    return `#/sessions/${a.key.session_key}/actions/${a.key.action_seq}`;
  }
</script>

<header class="app-header px-4 py-3 sm:px-6 flex items-center gap-3">
  <h1 class="app-header-title text-lg font-semibold m-0">Airlock</h1>
  {#if pending.length > 0}
    <span class="badge text-xs font-bold rounded-full px-2.5 py-0.5">{pending.length} pending</span>
  {/if}
</header>

<main class="max-w-4xl mx-auto px-4 py-6">
  <h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-3 mt-6 first:mt-0">Pending approval</h2>
  {#if pending.length === 0}
    <p class="section-heading italic py-4">No pending actions.</p>
  {:else}
    <div class="overflow-x-auto rounded-lg shadow-sm">
      <table class="w-full text-sm">
        <thead>
          <tr class="thead-row">
            <th class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider">Server</th>
            <th class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider">Tool</th>
            <th
              class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider hidden sm:table-cell"
              >Justification</th
            >
            <th
              class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider hidden md:table-cell"
              >Created</th
            >
          </tr>
        </thead>
        <tbody class="tbody">
          {#each pending as a (`${a.key.session_key}/${a.key.action_seq}`)}
            <tr class="data-row transition-colors">
              <td class="px-4 py-2.5"
                ><code class="code-tag text-xs rounded px-1.5 py-0.5">{a.call.server_namespace}</code></td
              >
              <td class="px-4 py-2.5 font-mono text-sm"
                ><a href={actionHref(a)} class="theme-link">{a.call.tool_name}</a></td
              >
              <td class="px-4 py-2.5 hidden sm:table-cell" style="color: var(--color-text);">{a.justification}</td>
              <td class="px-4 py-2.5 text-xs hidden md:table-cell" style="color: var(--color-text-muted);"
                >{fmt(a.created_at)}</td
              >
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-3 mt-8">Recent actions</h2>
  {#if recent.length === 0}
    <p class="section-heading italic py-4">No recent actions.</p>
  {:else}
    <div class="overflow-x-auto rounded-lg shadow-sm">
      <table class="w-full text-sm">
        <thead>
          <tr class="thead-row">
            <th class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider">Server</th>
            <th class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider">Tool</th>
            <th class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider">Status</th>
            <th
              class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider hidden sm:table-cell"
              >Justification</th
            >
            <th
              class="th-cell text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider hidden md:table-cell"
              >Updated</th
            >
          </tr>
        </thead>
        <tbody class="tbody">
          {#each recent as a (`${a.key.session_key}/${a.key.action_seq}`)}
            <tr class="data-row transition-colors">
              <td class="px-4 py-2.5"
                ><code class="code-tag text-xs rounded px-1.5 py-0.5">{a.call.server_namespace}</code></td
              >
              <td class="px-4 py-2.5 font-mono text-sm"
                ><a href={actionHref(a)} class="theme-link">{a.call.tool_name}</a></td
              >
              <td class="px-4 py-2.5"><span class="status-pill status-pill-{a.state.status}">{a.state.status}</span></td
              >
              <td class="px-4 py-2.5 hidden sm:table-cell" style="color: var(--color-text);">{a.justification}</td>
              <td class="px-4 py-2.5 text-xs hidden md:table-cell" style="color: var(--color-text-muted);"
                >{fmt(a.updated_at)}</td
              >
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</main>
