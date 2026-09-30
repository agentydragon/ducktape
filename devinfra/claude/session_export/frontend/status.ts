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

/** Server messages are written for logs, lower-case; the page shows them as sentences. */
export function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
