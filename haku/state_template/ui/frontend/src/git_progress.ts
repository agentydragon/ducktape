import { useSyncExternalStore } from "react";

// Progress across the repo's git primitives (repo.ts). Every tree/blob read runs through
// `gitProgress.track`, so one global indicator can mirror git's own object-transfer progress
// ("Fetching objects: n/m") across the whole app instead of each view spinning its own bar.
//
// Why a module-level singleton (not React state/context): the git primitives are plain async
// functions, not components, so they can't reach a Context — yet both they (writing progress) and
// the bar (reading it) need one shared source. That's exactly a `useSyncExternalStore` store, and
// it matches repo.ts's own module-singleton caches. `createStore` keeps the mutable pieces — the
// snapshot, the settle timer, the listeners — private to the one instance rather than loose
// module-level bindings.
//
// A *burst* is a run of activity: operation counts (each `track` call — a tree read or one ≤50-blob
// chunk) and git-object counts (a tree is 1, a bulk read is one per sha) accumulate over it. When
// the last op finishes the burst lingers for SETTLE_MS: a `docsUnder` read fires its tree then
// several blob chunks with only synchronous gaps between them, so the window keeps them in ONE
// burst and the bar fills smoothly instead of restarting per chunk. Counters mutate synchronously
// in start/finally, consistent under JS's single thread.

export interface GitProgress {
  activeOps: number; // tree/blob fetches currently in flight
  doneOps: number; // fetches completed this burst
  totalObjects: number; // git objects requested this burst (tree = 1, blobs = one per sha)
  doneObjects: number; // git objects whose fetch has resolved (or errored out)
}

const IDLE: GitProgress = { activeOps: 0, doneOps: 0, totalObjects: 0, doneObjects: 0 };
const SETTLE_MS = 150;

function createStore() {
  let state: GitProgress = IDLE;
  let settleTimer: ReturnType<typeof setTimeout> | null = null;
  const listeners = new Set<() => void>();

  const emit = (next: GitProgress): void => {
    state = next;
    for (const notify of listeners) notify();
  };

  return {
    // Live snapshot — referentially stable between emits, as useSyncExternalStore requires.
    get: (): GitProgress => state,

    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },

    // Run a git primitive, accounting for its `objects` git objects in the shared burst. The
    // finally drains whether `run` resolves or throws, then schedules the settle once idle.
    async track<T>(objects: number, run: () => Promise<T>): Promise<T> {
      if (settleTimer !== null) {
        clearTimeout(settleTimer); // new activity extends the burst — cancel the pending settle
        settleTimer = null;
      }
      emit({ ...state, activeOps: state.activeOps + 1, totalObjects: state.totalObjects + objects });
      try {
        return await run();
      } finally {
        const activeOps = state.activeOps - 1;
        emit({
          activeOps,
          doneOps: state.doneOps + 1,
          totalObjects: state.totalObjects,
          doneObjects: state.doneObjects + objects,
        });
        if (activeOps === 0) {
          settleTimer = setTimeout(() => {
            settleTimer = null;
            if (state.activeOps === 0) emit(IDLE); // still idle after the window → end the burst
          }, SETTLE_MS);
        }
      }
    },
  };
}

// One app-wide instance: there's one git read layer and one indicator.
export const gitProgress = createStore();

export function useGitProgress(): GitProgress {
  return useSyncExternalStore(gitProgress.subscribe, gitProgress.get);
}
