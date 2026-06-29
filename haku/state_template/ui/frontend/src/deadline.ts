// Live deadline countdown: classify how close an item's deadline is and format it
// compactly, so the UI can foreground time-critical items and tick them down.

export type Urgency = "overdue" | "soon" | "later";

export interface Countdown {
  text: string;
  urgency: Urgency;
}

const MIN = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

// Items within this many days of their deadline (or already past it) are "due soon".
export const DUE_SOON_DAYS = 7;

// Compact duration: the top one-or-two units (e.g. "2d 4h", "18h", "45m", "<1m").
function fmt(ms: number): string {
  const d = Math.floor(ms / DAY);
  const h = Math.floor((ms % DAY) / HOUR);
  const m = Math.floor((ms % HOUR) / MIN);
  if (d > 0) return h > 0 ? `${d}d ${h}h` : `${d}d`;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return `${m}m`;
  return "<1m";
}

export function countdown(deadlineIso: string, nowMs: number): Countdown {
  const diff = new Date(deadlineIso).getTime() - nowMs;
  if (diff < 0) return { text: `OVERDUE by ${fmt(-diff)}`, urgency: "overdue" };
  return { text: `due in ${fmt(diff)}`, urgency: diff <= DUE_SOON_DAYS * DAY ? "soon" : "later" };
}
