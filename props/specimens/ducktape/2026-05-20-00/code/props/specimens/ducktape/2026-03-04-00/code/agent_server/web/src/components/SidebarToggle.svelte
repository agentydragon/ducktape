<script lang="ts">
  export let title: string = "";
  export let label: string = "";
  export let glyph: string = "«";
  export let action: (() => void) | null = null;
  export let extraClass: string = "";
</script>

<button class="toggle-btn {extraClass}" {title} aria-label={label} on:click={() => action && action()}>
  {glyph}
</button>

<style>
  .toggle-btn {
    width: 28px;
    height: 22px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    background: var(--surface-2);
    font-weight: 700;
    cursor: pointer;
  }
</style>
