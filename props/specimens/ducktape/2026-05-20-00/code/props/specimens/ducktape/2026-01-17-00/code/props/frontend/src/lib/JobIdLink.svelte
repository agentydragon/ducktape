<script lang="ts">
  // Display component for job IDs (UUIDs)
  // - Truncates to 8 chars with CSS
  // - Monospace font
  // - Not clickable (jobs aren't navigable entities)

  interface Props {
    id: string;
  }

  let { id }: Props = $props();
</script>

<span class="font-mono text-gray-500 truncate" title={id}>
  {id.slice(0, 8)}...
</span>
