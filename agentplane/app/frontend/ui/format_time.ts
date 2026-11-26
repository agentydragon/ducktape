import { formatDuration, intervalToDuration } from "date-fns";

/** The time of day a timestamp falls at, to the minute, in the viewer's locale. */
export function clockTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Compact elapsed time for the fixed-width activity column. */
export function timeAgo(value: string | number | Date, now: number = Date.now()): string {
  const seconds = Math.trunc((now - new Date(value).getTime()) / 1000);
  for (const [period, unit] of [
    [86400, "d"],
    [3600, "h"],
    [60, "min"],
    [1, "s"],
  ] as const) {
    if (Math.abs(seconds) >= period) {
      const label = `${Math.floor(Math.abs(seconds) / period)} ${unit}`;
      return seconds < 0 ? `in ${label}` : `${label} ago`;
    }
  }
  return "now";
}

/** The full timestamp a relative time stands for, for the reader who hovers it. */
export function isoTime(value: string | number | Date): string {
  return new Date(value).toISOString();
}

/** A length of time a reader takes in at a glance: "1 hour 30 minutes", not the 5400 seconds behind it. */
export function durationLabel(seconds: number): string {
  return formatDuration(intervalToDuration({ start: 0, end: seconds * 1000 }));
}
