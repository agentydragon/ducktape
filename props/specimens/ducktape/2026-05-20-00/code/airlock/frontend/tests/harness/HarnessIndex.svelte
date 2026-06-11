<script lang="ts">
  let { pages, error }: { pages: string[]; error: string | null } = $props();
</script>

<div>
  {#if error}
    <p class="error">Unknown page: {error}</p>
  {/if}
  <h1>Airlock Visual Test Harness</h1>
  <ul>
    {#each pages as name}
      <li><a href="?page={name}">{name}</a></li>
    {/each}
  </ul>
</div>

<style>
  div {
    font-family: system-ui;
    padding: 20px;
  }
  .error {
    color: red;
  }
</style>
