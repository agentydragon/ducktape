import { Progress } from "@mantine/core";

import { useGitProgress } from "./git_progress.ts";

// Global transfer indicator for repo reads: a thin bar pinned to the top of the viewport that
// mirrors git's object-transfer progress (done/total) whenever any tree/blob fetch is in flight
// (or just settling), anywhere in the app. Idle → renders nothing. This is the one app-wide
// "talking to the repo" signal; individual surfaces no longer carry their own loading bar.
export function GitProgressBar() {
  const { total, done } = useGitProgress();
  if (total === 0) return null; // idle burst
  // Floor the width so the bar is visible the instant a burst starts (done === 0 → 0% is invisible).
  const pct = Math.max(8, Math.round((done / total) * 100));
  return (
    <Progress
      value={pct}
      size="xs"
      radius={0}
      striped
      animated
      transitionDuration={200}
      aria-label={`Fetching repository content: ${done}/${total} objects`}
      style={{ position: "fixed", top: 0, left: 0, right: 0, zIndex: 1000 }}
    />
  );
}
