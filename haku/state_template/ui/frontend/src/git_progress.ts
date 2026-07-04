// Central progress tracker for the repo's git primitives (repo.ts). Every tree/blob read registers
// here, so one global indicator can mirror git's own object-transfer progress ("Fetching objects:
// n/m") across the whole app — instead of each view spinning its own local bar. The two git
// primitives (recursive tree, bulk blobs) are the only I/O the UI does, so wrapping them captures
// all of it — item reads, garden files, improvements, responses, kitchen — at one choke point.
//
// A *burst* is a run of activity: its object counts accumulate (a tree is one object; a bulk-blob
// read is one per sha) and the bar climbs done/total as each fetch resolves. When the last fetch
// completes the burst doesn't reset instantly — it lingers for SETTLE_MS. A `docsUnder` read fires
// its tree then several ≤50-blob chunks back-to-back with only synchronous gaps between them, so
// the settle window keeps them in ONE burst: the bar fills smoothly to 100% instead of sawtoothing
// per chunk. If nothing new starts, the settle fires and clears the bar. Counters are plain module
// state mutated synchronously in start/finally, consistent under JS's single thread.

import { useSyncExternalStore } from "react";

export interface GitProgress {
  inFlight: number; // active tree/blob fetches
  total: number; // git objects requested in the current burst
  done: number; // objects whose fetch has resolved (or errored out)
}

const IDLE: GitProgress = { inFlight: 0, total: 0, done: 0 };
const SETTLE_MS = 150;

let state: GitProgress = IDLE;
let settleTimer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<() => void>();

function set(next: GitProgress): void {
  state = next;
  for (const notify of listeners) notify();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// Live snapshot — the store's single source of truth. `useGitProgress` reads it through React;
// tests read it directly. Referentially stable between `set`s, as useSyncExternalStore requires.
export function getGitProgress(): GitProgress {
  return state;
}

// Run a git primitive while accounting for its `objects` git objects in the shared burst. The
// finally drains whether `run` resolves or throws — the indicator must clear on error too — and
// when the last op finishes, schedules the settle that ends the burst.
export async function trackGit<T>(objects: number, run: () => Promise<T>): Promise<T> {
  // New activity extends the burst — cancel any pending settle so back-to-back reads stay merged.
  if (settleTimer !== null) {
    clearTimeout(settleTimer);
    settleTimer = null;
  }
  set({ inFlight: state.inFlight + 1, total: state.total + objects, done: state.done });
  try {
    return await run();
  } finally {
    const inFlight = state.inFlight - 1;
    set({ inFlight, total: state.total, done: state.done + objects });
    if (inFlight === 0) {
      settleTimer = setTimeout(() => {
        settleTimer = null;
        if (state.inFlight === 0) set(IDLE); // still idle after the window → end the burst
      }, SETTLE_MS);
    }
  }
}

export function useGitProgress(): GitProgress {
  return useSyncExternalStore(subscribe, getGitProgress);
}
