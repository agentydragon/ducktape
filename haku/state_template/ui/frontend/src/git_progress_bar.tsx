import { Progress } from "@mantine/core";

import { useGitProgress } from "./git_progress.ts";

// Global transfer indicator for repo reads: a thin bar pinned to the top of the viewport that
// mirrors git's object-transfer progress (done/total) whenever any tree/blob fetch is in flight,
// anywhere in the app. Idle → renders nothing. This is the app-wide "talking to the repo" signal;
// the per-view "Loading…" placeholders stay as region skeletons for a surface's first paint.
export function GitProgressBar() {
  const { inFlight, total, done } = useGitProgress();
  if (inFlight === 0) return null;
  // Floor the width so the bar is visible the instant a burst starts (done === 0 → 0% is invisible).
  const pct = total > 0 ? Math.max(8, Math.round((done / total) * 100)) : 8;
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
