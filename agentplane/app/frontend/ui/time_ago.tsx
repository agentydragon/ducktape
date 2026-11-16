/** A datetime as "4 minutes ago", the full ISO datetime on hover; it re-renders as the time passes. */
import { type JSX, useSyncExternalStore } from "react";

import { isoTime, timeAgo } from "./format_time";

const REFRESH_MS = 30_000;

const listeners = new Set<() => void>();
let tick = 0;
let timer: number | undefined;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === undefined) {
    timer = window.setInterval(() => {
      tick++;
      for (const notify of listeners) notify();
    }, REFRESH_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      window.clearInterval(timer);
      timer = undefined;
    }
  };
}

export function TimeAgo({ value }: { value: string | number | Date }): JSX.Element {
  useSyncExternalStore(subscribe, () => tick);
  return (
    <time dateTime={isoTime(value)} title={isoTime(value)}>
      {timeAgo(value)}
    </time>
  );
}
