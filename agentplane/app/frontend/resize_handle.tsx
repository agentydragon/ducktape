import { useLocalStorage } from "@mantine/hooks";
import { type JSX, type PointerEvent, useRef } from "react";

const KEYBOARD_STEP = 16;

/** A pane's user-resized size, a per-viewer convenience persisted to `localStorage`. */
export interface PaneSize {
  readonly size: number;
  readonly min: number;
  readonly max: number;
  resize(size: number): void;
  step(delta: number): void;
}

export function usePaneSize(key: string, initial: number, min: number, max: number): PaneSize {
  const clamp = (size: number): number => Math.min(max, Math.max(min, size));
  const [size, setSize] = useLocalStorage({
    key,
    defaultValue: clamp(initial),
    getInitialValueInEffect: false,
    serialize: String,
    deserialize: (stored) => {
      const parsed = Number(stored);
      return Number.isFinite(parsed) ? clamp(parsed) : clamp(initial);
    },
  });
  return {
    size,
    min,
    max,
    resize: (next) => setSize(clamp(next)),
    // Relative to the latest state, not `size`: key repeats can land faster than a re-render.
    step: (delta) => setSize((current) => clamp(current + delta)),
  };
}

/** A drag or arrow-key handle resizing `pane`: on `x` a column's trailing edge, on `y` a panel's top
 * edge, so dragging right or up grows it. */
export function ResizeHandle({
  pane,
  axis,
  ...attributes
}: {
  pane: PaneSize;
  axis: "x" | "y";
  className: string;
  "aria-label": string;
  "aria-controls"?: string;
}): JSX.Element {
  const drag = useRef<{ pointerId: number; start: number; startSize: number } | null>(null);
  const along = (event: PointerEvent): number => (axis === "x" ? event.clientX : -event.clientY);
  const [grow, shrink] = axis === "x" ? ["ArrowRight", "ArrowLeft"] : ["ArrowUp", "ArrowDown"];
  return (
    <div
      {...attributes}
      role="separator"
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-valuenow={pane.size}
      aria-valuemin={pane.min}
      aria-valuemax={pane.max}
      tabIndex={0}
      onPointerDown={(event) => {
        drag.current = { pointerId: event.pointerId, start: along(event), startSize: pane.size };
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={(event) => {
        const current = drag.current;
        if (current?.pointerId === event.pointerId) pane.resize(current.startSize + along(event) - current.start);
      }}
      onPointerUp={(event) => {
        if (drag.current?.pointerId !== event.pointerId) return;
        drag.current = null;
        event.currentTarget.releasePointerCapture?.(event.pointerId);
      }}
      onKeyDown={(event) => {
        if (event.key !== grow && event.key !== shrink) return;
        event.preventDefault();
        pane.step(event.key === grow ? KEYBOARD_STEP : -KEYBOARD_STEP);
      }}
    />
  );
}
