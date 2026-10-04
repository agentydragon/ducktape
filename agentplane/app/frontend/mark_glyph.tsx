/** Draws a StatusMark in the page: its icon, in its color. status_mark.ts decides which and what color; this only renders it. */
import type { JSX } from "react";

import type { StatusMark } from "./status_mark";
import "./mark_glyph.css";

export function MarkGlyph({ mark, size = "regular" }: { mark: StatusMark; size?: "small" | "regular" }): JSX.Element {
  const { icon: Icon, color } = mark;
  return (
    <span className={size === "small" ? "agentplane-mark agentplane-mark-small" : "agentplane-mark"} style={{ color }}>
      <Icon />
    </span>
  );
}
