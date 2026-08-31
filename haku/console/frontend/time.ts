import {
  differenceInMilliseconds,
  differenceInSeconds,
  format,
  formatDistanceStrict,
  formatDuration,
  intervalToDuration,
  minutesToMilliseconds,
  parseISO,
  secondsToMilliseconds,
} from "date-fns";

/** Shared concise formatting for wall-clock instants in the console. `date-fns` owns the wording
 * and rounding; relative values are useful while triaging live state, and the complete locale
 * value stays available as the element title. */
export type TimestampDisplay = { text: string; title: string; isFresh: boolean };

const FRESHNESS_WINDOW_MS = minutesToMilliseconds(1);

export function parseTimestamp(value: string): Date {
  return parseISO(value);
}

export function shortDate(value: string | null | undefined): string | null {
  if (!value) return null;
  return format(parseTimestamp(value), "PPp");
}

/** A timestamp in a concise, human-readable form with the full date and time as `title`. */
export function formatTimestamp(value: string, nowMs: number = Date.now()): TimestampDisplay {
  const date = parseTimestamp(value);
  const now = new Date(nowMs);
  return {
    text: formatDistanceStrict(date, now, { addSuffix: true, roundingMethod: "round" }),
    title: format(date, "PPp"),
    isFresh: Math.abs(differenceInMilliseconds(date, now)) < FRESHNESS_WINDOW_MS,
  };
}

export function formatClockTime(value: Date): string {
  return format(value, "p");
}

export function formatDurationShort(seconds: number): string {
  const duration = formatDuration(
    intervalToDuration({ start: 0, end: secondsToMilliseconds(Math.max(0, Math.round(seconds))) }),
    {
      format: ["days", "hours", "minutes"],
      delimiter: " ",
    }
  );
  return duration || "0 minutes";
}

export function formatWindowDuration(seconds: number): string {
  const duration = formatDuration(
    intervalToDuration({ start: 0, end: secondsToMilliseconds(Math.max(0, Math.round(seconds))) }),
    {
      format: ["days", "hours"],
      delimiter: " ",
    }
  );
  return duration || "0 hours";
}

export function secondsUntil(value: string, nowMs: number = Date.now()): number {
  return Math.max(0, differenceInSeconds(parseTimestamp(value), new Date(nowMs), { roundingMethod: "round" }));
}
