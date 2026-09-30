/** Wording for the moments the status carries, counted from a `now` the caller passes so the page can tick. */

const MINUTE = 60_000;
function span(milliseconds: number): string {
  const minutes = Math.round(Math.max(0, milliseconds) / MINUTE);
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** An access token is refreshed shortly before it lapses, so a past expiry means a refresh is due. */
export function untilExpiry(expiresAt: string, now: number): string {
  const remaining = Date.parse(expiresAt) - now;
  return remaining <= 0 ? "due for refresh" : `in ${span(remaining)}`;
}

export function ago(instant: string, now: number): string {
  const elapsed = now - Date.parse(instant);
  return elapsed < MINUTE ? "just now" : `${span(elapsed)} ago`;
}

/** A polling interval as prose: "45 seconds", "5 minutes", "2 hours". */
export function every(seconds: number): string {
  if (seconds < 60) return plural(Math.round(seconds), "second");
  if (seconds < 3600) return plural(Math.round(seconds / 60), "minute");
  return plural(Math.round(seconds / 3600), "hour");
}

export function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** The poll starts a fixed pause after the last one finished. */
export function nextPoll(finishedAt: string, intervalSeconds: number, now: number): string {
  const remaining = Date.parse(finishedAt) + intervalSeconds * 1000 - now;
  return remaining <= 0 ? "due now" : `in ${span(remaining)}`;
}

/** When an event last arrived over a stream, or that none has since the sync started. */
export function lastEvent(instant: string | null, now: number): string {
  return instant === null ? "none yet" : ago(instant, now);
}

/** Server messages are written for logs, lower-case; the page shows them as sentences. */
export function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
