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

<header class="bg-blue-800 px-4 py-3 sm:px-6 flex items-center gap-3">
  <h1 class="text-white text-lg font-semibold m-0">
    <a href="#/" class="text-blue-200 hover:text-white hover:underline">Approval Gate</a>
    <span class="text-blue-300 font-normal"> / </span>
    <span class="text-sm font-normal text-blue-100">Action {action.key.session_key}/{action.key.action_seq}</span>
  </h1>
</header>

<main class="max-w-4xl mx-auto px-4 py-6 space-y-4">
  <div class="bg-white rounded-lg shadow-sm p-5">
    <h2 class="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-4">Details</h2>
    <dl class="grid grid-cols-1 sm:grid-cols-[140px_1fr] gap-x-4 gap-y-2 text-sm">
      <dt class="font-semibold text-gray-500">Session</dt>
      <dd class="m-0 break-all">
        <code class="text-xs bg-slate-100 rounded px-1.5 py-0.5">{action.key.session_key}</code>
      </dd>
      <dt class="font-semibold text-gray-500">Action #</dt>
      <dd class="m-0"><code class="text-xs bg-slate-100 rounded px-1.5 py-0.5">{action.key.action_seq}</code></dd>
      <dt class="font-semibold text-gray-500">Status</dt>
      <dd class="m-0"><span class="status-pill status-pill-{action.state.status}">{action.state.status}</span></dd>
      <dt class="font-semibold text-gray-500">Server</dt>
      <dd class="m-0">
        <code class="text-xs bg-slate-100 rounded px-1.5 py-0.5">{action.call.server_namespace}</code>
      </dd>
      <dt class="font-semibold text-gray-500">Tool</dt>
      <dd class="m-0"><code class="text-xs bg-slate-100 rounded px-1.5 py-0.5">{action.call.tool_name}</code></dd>
      <dt class="font-semibold text-gray-500">Justification</dt>
      <dd class="m-0 text-gray-700">{action.justification}</dd>
      <dt class="font-semibold text-gray-500">Created</dt>
      <dd class="m-0 text-gray-600">{fmt(action.created_at)}</dd>
      <dt class="font-semibold text-gray-500">Updated</dt>
      <dd class="m-0 text-gray-600">{fmt(action.updated_at)}</dd>
      {#if rejectedReason}
        <dt class="font-semibold text-red-600">Reject reason</dt>
        <dd class="m-0 text-red-700">{rejectedReason}</dd>
      {/if}
    </dl>
  </div>

  <div class="bg-white rounded-lg shadow-sm p-5">
    <h2 class="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-4">Arguments</h2>
    <pre class="bg-slate-50 rounded-lg p-4 overflow-x-auto text-sm font-mono text-gray-800 m-0">{JSON.stringify(
        action.call.arguments,
        null,
        2
      )}</pre>
  </div>

  {#if doneOutcome}
    <div class="bg-white rounded-lg shadow-sm p-5">
      <h2 class="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-4">Outcome</h2>
      {#if !doneOutcome.isError}
        <p class="text-green-700 font-semibold mb-3">&#10003; Executed successfully</p>
        <pre class="bg-slate-50 rounded-lg p-4 overflow-x-auto text-sm font-mono text-gray-800 m-0">{JSON.stringify(
            doneOutcome.content,
            null,
            2
          )}</pre>
      {:else}
        <p class="text-red-600 font-semibold mb-3">&#10007; Execution failed</p>
        <pre class="bg-slate-50 rounded-lg p-4 overflow-x-auto text-sm font-mono text-gray-800 m-0">{JSON.stringify(
            doneOutcome.content,
            null,
            2
          )}</pre>
      {/if}
    </div>
  {/if}

  {#if action.state.status === "pending"}
    <div class="bg-white rounded-lg shadow-sm p-5">
      <h2 class="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-4">Decision</h2>
      <div class="flex flex-col sm:flex-row gap-4">
        <button
          class="bg-green-600 hover:bg-green-700 disabled:bg-green-300 text-white font-semibold px-5 py-2.5 rounded-lg border-0 cursor-pointer transition-colors"
          disabled={approving}
          onclick={onApprove}
        >
          &#10003; Approve &amp; Execute
        </button>
        <div class="flex-1">
          <label for="reason" class="text-sm text-gray-600">Reason (optional)</label>
          <textarea
            id="reason"
            rows={2}
            placeholder="Optional rejection reason…"
            bind:value={rejectReason}
            class="w-full mt-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          ></textarea>
          <button
            class="mt-2 bg-red-600 hover:bg-red-700 disabled:bg-red-300 text-white font-semibold px-5 py-2.5 rounded-lg border-0 cursor-pointer transition-colors"
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
