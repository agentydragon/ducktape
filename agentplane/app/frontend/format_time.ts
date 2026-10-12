import { intlFormatDistance } from "date-fns";

/** The time of day a timestamp falls at, to the minute, in the viewer's locale. */
export function clockTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** An age a reader takes in at a glance: "40 minutes ago", not the 2417 seconds behind it. */
export function ageAgo(seconds: number): string {
  const now = Date.now();
  return intlFormatDistance(now - seconds * 1000, now, { locale: "en" });
}
