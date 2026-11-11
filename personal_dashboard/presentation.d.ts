export declare function formatLocalTimestamp(
  value: string | Date | null | undefined,
  now?: Date,
  details?: boolean
): string | null;

export declare function formatCalendarDate(value: string | null | undefined, now?: Date): string | null;

export declare function formatLocalClock(instant: Date): string;

export declare function formatLocalWeekday(instant: Date): string;

export declare function formatPeakInterval(interval: { start: string; end: string }): string;
