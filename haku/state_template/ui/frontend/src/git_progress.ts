// Central progress tracker for the repo's git primitives (repo.ts). Every tree/blob read
// registers here, so a single global indicator can mirror git's own object-transfer progress
// ("Fetching objects: n/m") across the whole app — instead of each view spinning its own local
// "Loading…". The two git primitives (recursive tree, bulk blobs) are the only I/O the UI does,
// so wrapping them captures all of it — item reads, garden files, improvements, responses — at
// one choke point.
//
// A *burst* is the window from the first in-flight fetch to the moment the last one drains. Its
// object counts (a tree is one object; a bulk-blob read is one per sha) accumulate while anything
// is in flight and reset to zero once the burst empties, so the next activity starts a fresh
// 0→100%. Counters are plain module state mutated synchronously in start/finally, so overlapping
// bursts (several widgets mounting at once) stay consistent under JS's single thread.

import { useSyncExternalStore } from "react";

export interface GitProgress {
  inFlight: number; // active tree/blob fetches
  total: number; // git objects requested in the current burst
  done: number; // objects whose fetch has resolved (or errored out)
}

const IDLE: GitProgress = { inFlight: 0, total: 0, done: 0 };

let state: GitProgress = IDLE;
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
// snaps the whole burst back to IDLE when it empties.
export async function trackGit<T>(objects: number, run: () => Promise<T>): Promise<T> {
  set({ inFlight: state.inFlight + 1, total: state.total + objects, done: state.done });
  try {
    return await run();
  } finally {
    const inFlight = state.inFlight - 1;
    set(inFlight === 0 ? IDLE : { inFlight, total: state.total, done: state.done + objects });
  }
}

export function useGitProgress(): GitProgress {
  return useSyncExternalStore(subscribe, getGitProgress);
}
