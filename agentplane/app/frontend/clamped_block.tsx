import { Button, UnstyledButton } from "@mantine/core";
import { type JSX, type ReactNode, useLayoutEffect, useRef, useState } from "react";

/** How many lines `text` has, a final newline ending its last line rather than starting another. */
export function lineCount(text: string): number {
  return text.replace(/\n$/, "").split("\n").length;
}

/** Content capped at `maxHeightRem`, whose clipped bottom is itself the control that shows the rest.
 * Clipped, not sliced: the hidden part stays in the DOM, so selecting and copying still reach all
 * of it.
 *
 * `lines`, when the content is text, says how much the clipped bottom holds: "Show all 32 lines".
 *
 * `expansion` is a controlled `[expanded, setExpanded]` pair, which is what `useRetainedDisclosure`
 * returns, for a caller whose rows leave the DOM and must come back as the reader left them. Without
 * it the block remembers for as long as it stays mounted. */
export function ClampedBlock({
  maxHeightRem,
  lines,
  expansion,
  children,
}: {
  maxHeightRem: number;
  lines?: number;
  expansion?: readonly [boolean, (expanded: boolean) => void];
  children: ReactNode;
}): JSX.Element {
  const local = useState(false);
  const [expanded, setExpanded] = expansion ?? local;
  const content = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const element = content.current;
    if (!element) return;
    // The content's own height, measured outside the clipping box, against the cap in pixels.
    const measure = () => {
      const remPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      setOverflows(element.offsetHeight > maxHeightRem * remPx + 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [maxHeightRem]);
  const clipped = overflows && !expanded;
  return (
    <div>
      <div
        data-clamped={clipped}
        style={{
          position: "relative",
          overflow: "hidden",
          ...(clipped && { maxHeight: `${maxHeightRem}rem` }),
        }}
      >
        <div ref={content}>{children}</div>
        {clipped && (
          <UnstyledButton
            aria-expanded={false}
            onClick={() => setExpanded(true)}
            style={{
              position: "absolute",
              insetInline: 0,
              bottom: 0,
              height: "2.5rem",
              display: "flex",
              alignItems: "flex-end",
              justifyContent: "center",
              paddingBottom: 2,
              background: "linear-gradient(to bottom, transparent, var(--mantine-color-body) 85%)",
              color: "var(--mantine-color-dimmed)",
              fontSize: "var(--mantine-font-size-xs)",
            }}
          >
            {lines !== undefined && lines > 1 ? `Show all ${lines} lines` : "Show all"}
          </UnstyledButton>
        )}
      </div>
      {overflows && expanded && (
        <Button variant="subtle" size="compact-xs" mt={4} aria-expanded onClick={() => setExpanded(false)}>
          Show less
        </Button>
      )}
    </div>
  );
}
