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
      await (await getMcpClient()).approve(action.id);
    } catch (e) {
      alert(`Approve failed: ${String(e)}`);
      approving = false;
    }
  }

  async function onReject() {
    rejecting = true;
    try {
      await (await getMcpClient()).reject(action.id, rejectReason.trim() || undefined);
    } catch (e) {
      alert(`Reject failed: ${String(e)}`);
      rejecting = false;
    }
  }
</script>

<header>
  <h1><a href="#/">Approval Gate</a> / Action {action.id.slice(0, 8)}</h1>
</header>

<main>
  <div class="card">
    <h2>Details</h2>
    <dl>
      <dt>ID</dt>
      <dd><code>{action.id}</code></dd>
      <dt>Status</dt>
      <dd><span class="status status-{action.state.status}">{action.state.status}</span></dd>
      <dt>Server</dt>
      <dd><code>{action.call.server_namespace}</code></dd>
      <dt>Tool</dt>
      <dd><code>{action.call.tool_name}</code></dd>
      <dt>Justification</dt>
      <dd>{action.justification}</dd>
      <dt>Created</dt>
      <dd>{fmt(action.created_at)}</dd>
      <dt>Updated</dt>
      <dd>{fmt(action.updated_at)}</dd>
      {#if rejectedReason}
        <dt>Reject reason</dt>
        <dd>{rejectedReason}</dd>
      {/if}
    </dl>
  </div>

  <div class="card">
    <h2>Arguments</h2>
    <pre>{JSON.stringify(action.call.arguments, null, 2)}</pre>
  </div>

  {#if doneOutcome}
    <div class="card">
      <h2>Outcome</h2>
      {#if !doneOutcome.isError}
        <p class="outcome-success">&#10003; Executed successfully</p>
        <pre>{JSON.stringify(doneOutcome.content, null, 2)}</pre>
      {:else}
        <p class="outcome-failed">&#10007; Execution failed</p>
        <pre>{JSON.stringify(doneOutcome.content, null, 2)}</pre>
      {/if}
    </div>
  {/if}

  {#if action.state.status === "pending"}
    <div class="card">
      <h2>Decision</h2>
      <div class="actions">
        <button class="btn-approve" disabled={approving} onclick={onApprove}> &#10003; Approve &amp; Execute </button>
        <div>
          <label for="reason">Reason (optional)</label>
          <textarea id="reason" rows={2} placeholder="Optional rejection reason…" bind:value={rejectReason}></textarea>
          <button class="btn-reject" style="margin-top: 0.5rem" disabled={rejecting} onclick={onReject}>
            &#10007; Reject
          </button>
        </div>
      </div>
    </div>
  {/if}
</main>
