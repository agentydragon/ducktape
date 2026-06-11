<script lang="ts">
  import "../styles/shared.css";
  import { prefs } from "../shared/prefs";

  export let lastError: string | null = null;
  export let clearError: () => void;
  export let deleteCurrentAgent: () => void;
  let showDeleteConfirm = false;
</script>

<div class="settings">
  <h4>Settings</h4>
  <label><input type="checkbox" bind:checked={$prefs.renderMarkdown} /> Render assistant as Markdown</label>
  <div class="row">
    <button on:click={clearError} disabled={!lastError}>Clear error</button>
  </div>
  <div class="danger">
    <h4>Danger Zone</h4>
    <p class="meta">Delete this agent and all of its history.</p>
    {#if !showDeleteConfirm}
      <button on:click={() => (showDeleteConfirm = true)} style="color: #b00020">Delete Agent and All Data</button>
    {:else}
      <div class="row">
        <button class="danger" on:click={deleteCurrentAgent}>Confirm Delete</button>
        <button class="secondary" on:click={() => (showDeleteConfirm = false)}>Cancel</button>
      </div>
    {/if}
  </div>
</div>

<style>
  .settings h4 {
    margin: 0.25rem 0;
  }
  .danger {
    margin-top: 1rem;
    padding-top: 0.5rem;
    border-top: 1px solid var(--border);
  }
  .meta {
    color: var(--muted);
    font-size: 0.85rem;
  }
  .danger :global(.danger) {
    color: #b00020;
  }
</style>
