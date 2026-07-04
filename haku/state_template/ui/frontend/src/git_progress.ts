// Central progress tracker for the repo's git primitives (repo.ts). Every tree/blob read registers
// here, so one global indicator can mirror git's own object-transfer progress ("Fetching objects:
// n/m") across the whole app — instead of each view spinning its own local bar. The two git
// primitives (recursive tree, bulk blobs) are the only content I/O the UI does, and every surface
// composes over them (items, garden, improvements, kitchen, runs, responses), so wrapping them
// captures all of it at one choke point.
//
// A *burst* is a run of activity. Two things accumulate over it: the operation count (each
// trackGit call — a tree read or one ≤50-blob chunk — is one operation) and the git-object count
// (a tree is one object; a bulk-blob read is one per sha). The bar fills doneObjects/totalObjects
// and the summary reports operations in-flight vs done. When the last op finishes the burst doesn't
// reset instantly — it lingers for SETTLE_MS. A `docsUnder` read fires its tree then several blob
// chunks back-to-back with only synchronous gaps between them, so the settle window keeps them in
// ONE burst: the bar fills smoothly instead of restarting per chunk. Counters are plain module
// state mutated synchronously in start/finally, consistent under JS's single thread.

import { useSyncExternalStore } from "react";

export interface GitProgress {
  activeOps: number; // tree/blob fetches currently in flight
  doneOps: number; // fetches completed this burst
  totalObjects: number; // git objects requested this burst (tree = 1, blobs = one per sha)
  doneObjects: number; // git objects whose fetch has resolved (or errored out)
}

const IDLE: GitProgress = { activeOps: 0, doneOps: 0, totalObjects: 0, doneObjects: 0 };
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
  set({ ...state, activeOps: state.activeOps + 1, totalObjects: state.totalObjects + objects });
  try {
    return await run();
  } finally {
    const activeOps = state.activeOps - 1;
    set({
      activeOps,
      doneOps: state.doneOps + 1,
      totalObjects: state.totalObjects,
      doneObjects: state.doneObjects + objects,
    });
    if (activeOps === 0) {
      settleTimer = setTimeout(() => {
        settleTimer = null;
        if (state.activeOps === 0) set(IDLE); // still idle after the window → end the burst
      }, SETTLE_MS);
    }
  }
}

export function useGitProgress(): GitProgress {
  return useSyncExternalStore(subscribe, getGitProgress);
}
