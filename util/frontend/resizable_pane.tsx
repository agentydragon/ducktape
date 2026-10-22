import { useLocalStorage } from "@mantine/hooks";
import { type ComponentPropsWithoutRef, type JSX, type PointerEvent, useRef } from "react";

const KEYBOARD_STEP = 16;

/** A pane's currently displayed size and the bounds available for interaction. */
export interface PaneSize {
  readonly size: number;
  readonly min: number;
  readonly max: number;
  resize(size: number): void;
  step(delta: number): void;
}

/** Persists the preferred size while limiting the displayed size to the current layout bounds. */
export function usePaneSize(key: string, initial: number, min: number, max: number, effectiveMax = max): PaneSize {
  const clamp = (size: number, upperBound: number): number => Math.min(upperBound, Math.max(min, size));
  const [preferredSize, setPreferredSize] = useLocalStorage({
    key,
    defaultValue: clamp(initial, max),
    getInitialValueInEffect: false,
    serialize: String,
    deserialize: (stored) => {
      const parsed = Number(stored);
      return Number.isFinite(parsed) ? clamp(parsed, max) : clamp(initial, max);
    },
  });
  const currentMax = Math.min(max, Math.max(min, effectiveMax));
  return {
    size: Math.min(preferredSize, currentMax),
    min,
    max: currentMax,
    resize: (next) => setPreferredSize(clamp(next, currentMax)),
    step: (delta) => setPreferredSize((current) => clamp(Math.min(current, currentMax) + delta, currentMax)),
  };
}

/** A pointer and keyboard separator: x grows right, y grows up. */
export function ResizeHandle({
  pane,
  axis,
  onResizeStart,
  onResizeEnd,
  children,
  ...attributes
}: {
  pane: PaneSize;
  axis: "x" | "y";
  "aria-label": string;
  onResizeStart?: () => void;
  onResizeEnd?: () => void;
} & Omit<
  ComponentPropsWithoutRef<"div">,
  | "role"
  | "aria-label"
  | "tabIndex"
  | "onPointerDown"
  | "onPointerMove"
  | "onPointerUp"
  | "onPointerCancel"
  | "onLostPointerCapture"
  | "onKeyDown"
>): JSX.Element {
  const drag = useRef<{ pointerId: number; start: number; startSize: number } | null>(null);
  const along = (event: PointerEvent<HTMLDivElement>): number => (axis === "x" ? event.clientX : -event.clientY);
  const [grow, shrink] = axis === "x" ? ["ArrowRight", "ArrowLeft"] : ["ArrowUp", "ArrowDown"];
  const finishDrag = (pointerId: number, element?: HTMLDivElement): void => {
    const current = drag.current;
    if (current?.pointerId !== pointerId) return;
    drag.current = null;
    if (element?.hasPointerCapture?.(pointerId)) element.releasePointerCapture?.(pointerId);
    onResizeEnd?.();
  };

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
        if (event.button !== 0 || event.isPrimary === false) return;
        event.preventDefault();
        drag.current = { pointerId: event.pointerId, start: along(event), startSize: pane.size };
        event.currentTarget.setPointerCapture?.(event.pointerId);
        onResizeStart?.();
      }}
      onPointerMove={(event) => {
        const current = drag.current;
        if (current?.pointerId === event.pointerId) pane.resize(current.startSize + along(event) - current.start);
      }}
      onPointerUp={(event) => finishDrag(event.pointerId, event.currentTarget)}
      onPointerCancel={(event) => finishDrag(event.pointerId, event.currentTarget)}
      onLostPointerCapture={(event) => finishDrag(event.pointerId)}
      onKeyDown={(event) => {
        if (event.key === grow || event.key === shrink) {
          event.preventDefault();
          pane.step(event.key === grow ? KEYBOARD_STEP : -KEYBOARD_STEP);
        } else if (event.key === "Home") {
          event.preventDefault();
          pane.resize(pane.min);
        } else if (event.key === "End") {
          event.preventDefault();
          pane.resize(pane.max);
        }
      }}
    >
      {children}
    </div>
  );
}
