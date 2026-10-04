/** Draws a StatusMark in the page: a dot, moving chevrons, or an icon. status_mark.ts decides which and in what color; this only renders it. */
import type { CSSProperties, JSX } from "react";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconCircleX from "@tabler/icons-react/dist/esm/icons/IconCircleX.mjs";
import IconClock from "@tabler/icons-react/dist/esm/icons/IconClock.mjs";
import IconPlayerPause from "@tabler/icons-react/dist/esm/icons/IconPlayerPause.mjs";
import IconPlayerPlay from "@tabler/icons-react/dist/esm/icons/IconPlayerPlay.mjs";

import { CHEVRON_CYCLE_MS, type MarkShape, type StatusMark } from "./status_mark";
import "./mark_glyph.css";

const ICON_PX = 13;

// Chevrons are drawn in a 14x10 box, one every CHEVRON_PITCH. The track holds enough of them that
// shifting it right by one pitch looks the same as not shifting it, so the CSS animation can loop.
const CHEVRON_PITCH = 5;
const CHEVRON_X = [-7, -2, 3, 8, 13];

function Chevrons(): JSX.Element {
  return (
    <svg className="agentplane-mark-chevron-svg" viewBox="0 0 14 10" aria-hidden="true" focusable="false">
      <g
        className="agentplane-mark-chevron-track"
        style={
          {
            "--chevron-pitch": `${CHEVRON_PITCH}px`,
            animationDuration: `${CHEVRON_CYCLE_MS}ms`,
          } as CSSProperties
        }
      >
        {CHEVRON_X.map((x) => (
          <path key={x} d={`M${x} 1.5L${x + 3} 5L${x} 8.5`} />
        ))}
      </g>
    </svg>
  );
}

function Shape({ shape }: { shape: MarkShape }): JSX.Element {
  switch (shape) {
    case "dot":
      return <span className="agentplane-mark-disc" />;
    case "chevrons":
      return <Chevrons />;
    case "play":
      return <IconPlayerPlay size={ICON_PX} />;
    case "pause":
      return <IconPlayerPause size={ICON_PX} />;
    case "clock":
      return <IconClock size={ICON_PX} />;
    case "cross":
      return <IconCircleX size={ICON_PX} />;
  }
}

export function MarkGlyph({ mark, size = "regular" }: { mark: StatusMark; size?: "small" | "regular" }): JSX.Element {
  const className = ["agentplane-mark", `agentplane-mark-${mark.shape}`, size === "small" && "agentplane-mark-small"]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={className} style={{ color: mark.color }}>
      <Shape shape={mark.shape} />
    </span>
  );
}
