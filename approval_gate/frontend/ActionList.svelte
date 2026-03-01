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

<header>
  <h1>Approval Gate</h1>
  {#if pending.length > 0}
    <span class="badge">{pending.length} pending</span>
  {/if}
</header>

<main>
  <h2>Pending approval</h2>
  {#if pending.length === 0}
    <p class="empty">No pending actions.</p>
  {:else}
    <table>
      <thead>
        <tr><th>Server</th><th>Tool</th><th>Justification</th><th>Created</th></tr>
      </thead>
      <tbody>
        {#each pending as a (`${a.key.session_key}/${a.key.action_seq}`)}
          <tr>
            <td><code>{a.call.server_namespace}</code></td>
            <td class="tool-name"><a href={actionHref(a)}>{a.call.tool_name}</a></td>
            <td>{a.justification}</td>
            <td>{fmt(a.created_at)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <h2>Recent actions</h2>
  {#if recent.length === 0}
    <p class="empty">No recent actions.</p>
  {:else}
    <table>
      <thead>
        <tr><th>Server</th><th>Tool</th><th>Status</th><th>Justification</th><th>Updated</th></tr>
      </thead>
      <tbody>
        {#each recent as a (`${a.key.session_key}/${a.key.action_seq}`)}
          <tr>
            <td><code>{a.call.server_namespace}</code></td>
            <td class="tool-name"><a href={actionHref(a)}>{a.call.tool_name}</a></td>
            <td><span class="status status-{a.state.status}">{a.state.status}</span></td>
            <td>{a.justification}</td>
            <td>{fmt(a.updated_at)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</main>
