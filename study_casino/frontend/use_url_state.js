// Tiny URL-state hook backed by URLSearchParams + history.pushState.
//
// Persists the value at `?<key>=...`. On set, pushes a new history entry so
// browser back/forward navigates between view states. Other tabs / windows
// listening to `popstate` see the change. The default is what's used when
// the param is missing or unrecognised.
//
// This is intentionally NOT React Router — the casino has a flat view set
// (one top-level "view" + a sub-game inside CasinoView). A dependency would
// be overkill.

import { useCallback, useEffect, useState } from "react";

function readParam(key, allowed, fallback) {
  const value = new URLSearchParams(window.location.search).get(key);
  return allowed.includes(value) ? value : fallback;
}

export function useUrlState(key, allowed, fallback) {
  const [value, setValue] = useState(() => readParam(key, allowed, fallback));

  useEffect(() => {
    const onPopState = () => setValue(readParam(key, allowed, fallback));
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [key, allowed, fallback]);

  const set = useCallback(
    (next) => {
      if (!allowed.includes(next)) return;
      if (next === value) return;
      const url = new URL(window.location.href);
      if (next === fallback) {
        url.searchParams.delete(key);
      } else {
        url.searchParams.set(key, next);
      }
      window.history.pushState({}, "", url);
      setValue(next);
    },
    [key, allowed, fallback, value]
  );

  return [value, set];
}
