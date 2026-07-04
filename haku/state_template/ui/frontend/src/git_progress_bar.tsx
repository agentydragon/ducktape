import { Box, Group, Progress, Text } from "@mantine/core";

import { useGitProgress } from "./git_progress.ts";

// Global transfer indicator for repo reads: a thin bar pinned to the BOTTOM of the viewport,
// mirroring git's object-transfer progress whenever any tree/blob fetch is in flight (or settling),
// anywhere in the app. Idle → renders nothing. This is the one app-wide "talking to the repo"
// signal; individual surfaces no longer carry their own loading bar. Above the bar sits a compact
// summary of the burst: operations in flight vs done, and git objects fetched vs requested.
export function GitProgressBar() {
  const { activeOps, doneOps, totalObjects, doneObjects } = useGitProgress();
  if (totalObjects === 0) return null; // idle burst
  // Floor the width so the bar is visible the instant a burst starts (doneObjects 0 → 0% invisible).
  const pct = Math.max(8, Math.round((doneObjects / totalObjects) * 100));
  return (
    <Box
      style={{
        position: "fixed",
        bottom: 0,
        left: 0,
        right: 0,
        zIndex: 1000,
        background: "var(--mantine-color-body)",
        borderTop: "1px solid var(--mantine-color-default-border)",
      }}
    >
      <Group justify="space-between" wrap="nowrap" px="sm" py={4} gap="sm">
        <Text size="xs" c="dimmed" fw={600} style={{ whiteSpace: "nowrap" }}>
          Fetching from git…
        </Text>
        <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>
          {activeOps} in progress · {doneOps} done · {doneObjects}/{totalObjects} objects
        </Text>
      </Group>
      <Progress
        value={pct}
        size="xs"
        radius={0}
        striped
        animated
        transitionDuration={200}
        aria-label={`Fetching from git: ${activeOps} operations in progress, ${doneOps} done, ${doneObjects} of ${totalObjects} objects`}
      />
    </Box>
  );
}
