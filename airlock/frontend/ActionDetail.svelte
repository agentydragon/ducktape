<script lang="ts">
  import { getMcpClient } from "./mcp.ts";
  import type { Action, DoneState, RejectedState } from "./types.ts";

  let { action }: { action: Action } = $props();
  let rejectReason = $state("");
  let approving = $state(false);
  let rejecting = $state(false);

  const rejectedReason = $derived(action.state.status === "rejected" ? (action.state as RejectedState).reason : null);
  const doneOutcome = $derived(action.state.status === "done" ? (action.state as DoneState).outcome : null);

  function fmt(iso: string): string {
    return new Date(iso).toLocaleString();
  }

  async function onApprove() {
    approving = true;
    try {
      await (await getMcpClient()).approve(action.key);
    } catch (e) {
      alert(`Approve failed: ${String(e)}`);
      approving = false;
    }
  }

  async function onReject() {
    rejecting = true;
    try {
      await (await getMcpClient()).reject(action.key, rejectReason.trim() || undefined);
    } catch (e) {
      alert(`Reject failed: ${String(e)}`);
      rejecting = false;
    }
  }
</script>

<header class="app-header px-4 py-3 sm:px-6 flex items-center gap-3">
  <h1 class="text-lg font-semibold m-0" style="color: var(--color-header-text);">
    <a href="#/" class="app-header-link">Airlock</a>
    <span class="font-normal" style="color: var(--color-header-text-dim);"> / </span>
    <span class="text-sm font-normal" style="color: var(--color-header-link);"
      >Action {action.key.session_key}/{action.key.action_seq}</span
    >
  </h1>
</header>

<main class="max-w-4xl mx-auto px-4 py-6 space-y-4">
  <div class="card rounded-lg shadow-sm p-5">
    <h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-4">Details</h2>
    <dl class="grid grid-cols-1 sm:grid-cols-[140px_1fr] gap-x-4 gap-y-2 text-sm">
      <dt class="section-heading font-semibold">Session</dt>
      <dd class="m-0 break-all">
        <code class="code-tag text-xs rounded px-1.5 py-0.5">{action.key.session_key}</code>
      </dd>
      <dt class="section-heading font-semibold">Action #</dt>
      <dd class="m-0"><code class="code-tag text-xs rounded px-1.5 py-0.5">{action.key.action_seq}</code></dd>
      <dt class="section-heading font-semibold">Status</dt>
      <dd class="m-0"><span class="status-pill status-pill-{action.state.status}">{action.state.status}</span></dd>
      <dt class="section-heading font-semibold">Server</dt>
      <dd class="m-0">
        <code class="code-tag text-xs rounded px-1.5 py-0.5">{action.call.server_namespace}</code>
      </dd>
      <dt class="section-heading font-semibold">Tool</dt>
      <dd class="m-0"><code class="code-tag text-xs rounded px-1.5 py-0.5">{action.call.tool_name}</code></dd>
      <dt class="section-heading font-semibold">Justification</dt>
      <dd class="m-0" style="color: var(--color-text);">{action.justification}</dd>
      <dt class="section-heading font-semibold">Created</dt>
      <dd class="m-0" style="color: var(--color-text-muted);">{fmt(action.created_at)}</dd>
      <dt class="section-heading font-semibold">Updated</dt>
      <dd class="m-0" style="color: var(--color-text-muted);">{fmt(action.updated_at)}</dd>
      {#if rejectedReason}
        <dt class="font-semibold" style="color: var(--color-error);">Reject reason</dt>
        <dd class="m-0" style="color: var(--color-error);">{rejectedReason}</dd>
      {/if}
    </dl>
  </div>

  <div class="card rounded-lg shadow-sm p-5">
    <h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-4">Arguments</h2>
    <pre class="pre-block rounded-lg p-4 overflow-x-auto text-sm font-mono m-0">{JSON.stringify(
        action.call.arguments,
        null,
        2
      )}</pre>
  </div>

  {#if doneOutcome}
    <div class="card rounded-lg shadow-sm p-5">
      <h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-4">Outcome</h2>
      {#if !doneOutcome.isError}
        <p class="font-semibold mb-3" style="color: var(--color-success);">&#10003; Executed successfully</p>
        <pre class="pre-block rounded-lg p-4 overflow-x-auto text-sm font-mono m-0">{JSON.stringify(
            doneOutcome.content,
            null,
            2
          )}</pre>
      {:else}
        <p class="font-semibold mb-3" style="color: var(--color-error);">&#10007; Execution failed</p>
        <pre class="pre-block rounded-lg p-4 overflow-x-auto text-sm font-mono m-0">{JSON.stringify(
            doneOutcome.content,
            null,
            2
          )}</pre>
      {/if}
    </div>
  {/if}

  {#if action.state.status === "pending"}
    <div class="card rounded-lg shadow-sm p-5">
      <h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-4">Decision</h2>
      <div class="flex flex-col sm:flex-row gap-4">
        <button
          class="btn-approve font-semibold px-5 py-2.5 rounded-lg border-0 cursor-pointer transition-colors"
          disabled={approving}
          onclick={onApprove}
        >
          &#10003; Approve &amp; Execute
        </button>
        <div class="flex-1">
          <label for="reason" class="text-sm" style="color: var(--color-text-muted);">Reason (optional)</label>
          <textarea
            id="reason"
            rows={2}
            placeholder="Optional rejection reason…"
            bind:value={rejectReason}
            class="input-field w-full mt-1 rounded-lg px-3 py-2 text-sm"
          ></textarea>
          <button
            class="btn-reject mt-2 font-semibold px-5 py-2.5 rounded-lg border-0 cursor-pointer transition-colors"
            disabled={rejecting}
            onclick={onReject}
          >
            &#10007; Reject
          </button>
        </div>
      </div>
    </div>
  {/if}
</main>
