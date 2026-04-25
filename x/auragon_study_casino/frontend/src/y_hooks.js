// Tiny React hooks that re-render on Y.Map / Y.Array mutations.
//
// We deliberately don't use `useSyncExternalStore` here: the Y containers
// are mutable, so a referentially-stable snapshot would always be the
// same object and React would skip re-renders. The classic
// "force a render with a tick state" pattern is simplest and correct.

import { useEffect, useState } from "react";

import { casinoSync } from "./sync.js";

function useObservedYValue(target) {
  const [, forceRender] = useState(0);
  useEffect(() => {
    if (!target) return undefined;
    const fn = () => forceRender((t) => t + 1);
    target.observeDeep(fn);
    return () => target.unobserveDeep(fn);
  }, [target]);
  return target;
}

export function useYMap(ymap) {
  return useObservedYValue(ymap);
}

export function useYArray(yarr) {
  return useObservedYValue(yarr);
}

/** Subscribe to the global SyncStatus store. */
export function useSyncStatus() {
  const [state, setState] = useState(casinoSync.status.state);
  useEffect(() => casinoSync.status.subscribe(setState), []);
  return state;
}

/** Subscribe to the latest sync rejection (or null). */
export function useSyncRejection() {
  const [state, setState] = useState(casinoSync.rejection.state);
  useEffect(() => casinoSync.rejection.subscribe(setState), []);
  return state;
}
