import { Text } from "@mantine/core";

// Inline error for a surface that couldn't load its content — replaces the region. `what` names the
// surface ("the garden", "runs", "items"); it reads "Failed to load <what>: <error>". User-action
// failures use a toast instead (notifyError, errors.ts) so they don't blank a whole surface.
export function LoadError({ what, error }: { what: string; error: string }) {
  return (
    <Text c="red" my="lg">
      Failed to load {what}: {error}
    </Text>
  );
}
