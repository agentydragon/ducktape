<script lang="ts">
  export let label: string = "Close dialog";
  export let onClose: (() => void) | undefined;

  function handleClick(e: MouseEvent) {
    if (e.currentTarget === e.target) onClose && onClose();
  }
  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Escape" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onClose && onClose();
    }
  }
</script>

<div
  class="modal-backdrop"
  role="button"
  tabindex="0"
  aria-label={label}
  on:click={handleClick}
  on:keydown={handleKeydown}
>
  <slot />
</div>

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.35);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
</style>
