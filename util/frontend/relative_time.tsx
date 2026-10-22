import { type JSX, useSyncExternalStore } from "react";

const REFRESH_MS = 30_000;

export interface RelativeTimeDisplay {
  text: string;
  title?: string;
}

const listeners = new Set<() => void>();
let nowMs = Date.now();
let timer: number | undefined;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === undefined) {
    nowMs = Date.now();
    timer = window.setInterval(() => {
      nowMs = Date.now();
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

function getNowMs(): number {
  return nowMs;
}

export function RelativeTime({
  value,
  format,
}: {
  value: Date;
  format: (value: Date) => RelativeTimeDisplay;
}): JSX.Element {
  useSyncExternalStore(subscribe, getNowMs, getNowMs);
  const display = format(value);
  const dateTime = value.toISOString();
  return (
    <time dateTime={dateTime} title={display.title}>
      {display.text}
    </time>
  );
}
