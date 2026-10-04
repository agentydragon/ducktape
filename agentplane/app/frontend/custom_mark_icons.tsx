/** The two status marks no icon library supplies: a dot, and chevrons that move left to right. */
import type { CSSProperties, JSX } from "react";

import "./custom_mark_icons.css";

/** Time for the chevrons to advance one chevron pitch to the right. */
const CHEVRON_CYCLE_MS = 1_000;

// Chevrons are drawn in a 14x10 box, one every CHEVRON_PITCH. The track holds enough of them that
// shifting it right by one pitch looks the same as not shifting it, so the CSS animation can loop.
const CHEVRON_PITCH = 5;
const CHEVRON_X = [-7, -2, 3, 8, 13];

export function StatusDot(): JSX.Element {
  return <span className="agentplane-mark-disc" />;
}

export function RunningChevrons(): JSX.Element {
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
