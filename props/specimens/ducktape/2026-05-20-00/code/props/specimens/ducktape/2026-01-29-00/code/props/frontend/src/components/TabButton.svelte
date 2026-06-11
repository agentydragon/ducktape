<script lang="ts">
  interface Props {
    active: boolean;
    onclick: () => void;
    children: import("svelte").Snippet;
  }

  let { active, onclick, children }: Props = $props();
</script>

<button
  type="button"
  class="px-4 py-2 font-medium text-sm border-b-2 {active
    ? 'border-blue-500 text-blue-600'
    : 'border-transparent text-gray-500 hover:text-gray-700'}"
  {onclick}
>
  {@render children()}
</button>
