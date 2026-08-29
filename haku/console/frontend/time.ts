import { format, formatDistanceStrict } from "date-fns";

/** Shared concise formatting for wall-clock instants in the console. `date-fns` owns the wording
 * and rounding; relative values are useful while triaging live state, and the complete locale
 * value stays available as the element title. */
export type TimestampDisplay = { text: string; title: string; isFresh: boolean };

export function shortDate(value: string | null | undefined): string | null {
  if (!value) return null;
  return format(new Date(value), "PPp");
}

/** A timestamp in a concise, human-readable form with the full date and time as `title`. */
export function formatTimestamp(value: string, nowMs: number = Date.now()): TimestampDisplay {
  const date = new Date(value);
  return {
    text: formatDistanceStrict(date, new Date(nowMs), { addSuffix: true, roundingMethod: "round" }),
    title: format(date, "PPp"),
    isFresh: Math.abs(date.getTime() - nowMs) < 60_000,
  };
}
