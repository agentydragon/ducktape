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

<header class="bg-blue-800 px-4 py-3 sm:px-6 flex items-center gap-3">
  <h1 class="text-white text-lg font-semibold m-0">Approval Gate</h1>
  {#if pending.length > 0}
    <span class="bg-red-500 text-white text-xs font-bold rounded-full px-2.5 py-0.5">{pending.length} pending</span>
  {/if}
</header>

<main class="max-w-4xl mx-auto px-4 py-6">
  <h2 class="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-3 mt-6 first:mt-0">Pending approval</h2>
  {#if pending.length === 0}
    <p class="text-gray-400 italic py-4">No pending actions.</p>
  {:else}
    <div class="overflow-x-auto rounded-lg shadow-sm">
      <table class="w-full text-sm">
        <thead>
          <tr class="bg-slate-50 border-b border-slate-200">
            <th class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500">Server</th>
            <th class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500">Tool</th>
            <th
              class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500 hidden sm:table-cell"
              >Justification</th
            >
            <th
              class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500 hidden md:table-cell"
              >Created</th
            >
          </tr>
        </thead>
        <tbody class="bg-white divide-y divide-slate-100">
          {#each pending as a (`${a.key.session_key}/${a.key.action_seq}`)}
            <tr class="hover:bg-slate-50 transition-colors">
              <td class="px-4 py-2.5"
                ><code class="text-xs bg-slate-100 rounded px-1.5 py-0.5">{a.call.server_namespace}</code></td
              >
              <td class="px-4 py-2.5 font-mono text-sm"
                ><a href={actionHref(a)} class="text-blue-600 hover:text-blue-800 hover:underline">{a.call.tool_name}</a
                ></td
              >
              <td class="px-4 py-2.5 text-gray-600 hidden sm:table-cell">{a.justification}</td>
              <td class="px-4 py-2.5 text-gray-500 text-xs hidden md:table-cell">{fmt(a.created_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <h2 class="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-3 mt-8">Recent actions</h2>
  {#if recent.length === 0}
    <p class="text-gray-400 italic py-4">No recent actions.</p>
  {:else}
    <div class="overflow-x-auto rounded-lg shadow-sm">
      <table class="w-full text-sm">
        <thead>
          <tr class="bg-slate-50 border-b border-slate-200">
            <th class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500">Server</th>
            <th class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500">Tool</th>
            <th class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500">Status</th>
            <th
              class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500 hidden sm:table-cell"
              >Justification</th
            >
            <th
              class="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-slate-500 hidden md:table-cell"
              >Updated</th
            >
          </tr>
        </thead>
        <tbody class="bg-white divide-y divide-slate-100">
          {#each recent as a (`${a.key.session_key}/${a.key.action_seq}`)}
            <tr class="hover:bg-slate-50 transition-colors">
              <td class="px-4 py-2.5"
                ><code class="text-xs bg-slate-100 rounded px-1.5 py-0.5">{a.call.server_namespace}</code></td
              >
              <td class="px-4 py-2.5 font-mono text-sm"
                ><a href={actionHref(a)} class="text-blue-600 hover:text-blue-800 hover:underline">{a.call.tool_name}</a
                ></td
              >
              <td class="px-4 py-2.5"><span class="status-pill status-pill-{a.state.status}">{a.state.status}</span></td
              >
              <td class="px-4 py-2.5 text-gray-600 hidden sm:table-cell">{a.justification}</td>
              <td class="px-4 py-2.5 text-gray-500 text-xs hidden md:table-cell">{fmt(a.updated_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</main>
