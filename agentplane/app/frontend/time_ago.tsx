/** A datetime as "4 minutes ago", the full ISO datetime on hover; it re-renders as the time passes. */
import type { JSX } from "react";

import { isoTime, timeAgo } from "./format_time";
import { RelativeTime, type RelativeTimeDisplay } from "../../../util/frontend/relative_time";

function formatTimeAgo(value: Date): RelativeTimeDisplay {
  const timestamp = isoTime(value);
  return { text: timeAgo(value), title: timestamp };
}

export function TimeAgo({ value }: { value: string | number | Date }): JSX.Element {
  return <RelativeTime value={new Date(value)} format={formatTimeAgo} />;
}
