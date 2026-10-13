import { intlFormatDistance } from "date-fns";

/** The time of day a timestamp falls at, to the minute, in the viewer's locale. */
export function clockTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** A moment a reader takes in at a glance: "4 minutes ago" or "in 3 hours", not a clock reading. */
export function timeAgo(value: string | number | Date, now: number = Date.now()): string {
  return intlFormatDistance(value, now, { locale: "en" });
}

/** The full timestamp a relative time stands for, for the reader who hovers it. */
export function isoTime(value: string | number | Date): string {
  return new Date(value).toISOString();
}

/** An age a reader takes in at a glance: "40 minutes ago", not the 2417 seconds behind it. */
export function ageAgo(seconds: number): string {
  const now = Date.now();
  return timeAgo(now - seconds * 1000, now);
}
