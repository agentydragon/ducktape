import { UnstyledButton } from "@mantine/core";
import { type JSX, type ReactNode, useLayoutEffect, useState } from "react";

import { Disclosure, DisclosureBody } from "./disclosure";

/** Shared height limit for expandable text and output. */
export const TEXT_MAX_HEIGHT_REM = 16;

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
  label = "Expanded content",
  expandLabel,
  header,
  expansion,
  stickyCollapse = true,
  children,
}: {
  maxHeightRem: number;
  lines?: number;
  /** Short context for the sticky collapse control while expanded. */
  label?: string;
  /** Override the clipped-content action when a source line count is not meaningful. */
  expandLabel?: string;
  /** Existing heading to turn into the sticky control row when the block opens. */
  header?: ReactNode;
  expansion?: readonly [boolean, (expanded: boolean) => void];
  /** Use an enclosing Disclosure's sticky heading when this block is already nested in one. */
  stickyCollapse?: boolean;
  children: ReactNode;
}): JSX.Element {
  const local = useState(false);
  const [expanded, setExpanded] = expansion ?? local;
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const element = content;
    if (!element) return;
    // Measure the content outside its clipping box against the cap in pixels.
    const measure = () => {
      const remPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      setOverflows(element.offsetHeight > maxHeightRem * remPx + 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [content, maxHeightRem]);
  const clipped = overflows && !expanded;
  // Expansion is user state. CodeMirror briefly measures shorter while mounting inside the
  // sticky shell; retain its collapse control through that transient measurement.
  const body = (
    <>
      <div
        data-clamped={clipped}
        style={{
          position: "relative",
          overflow: "hidden",
          ...(clipped && { maxHeight: `${maxHeightRem}rem` }),
        }}
      >
        <div ref={setContent}>{children}</div>
        {clipped && (
          <div
            aria-hidden="true"
            style={{
              position: "absolute",
              insetInline: 0,
              bottom: 0,
              height: "2.5rem",
              pointerEvents: "none",
              background: "linear-gradient(to bottom, transparent, var(--mantine-color-body) 85%)",
            }}
          />
        )}
      </div>
      {clipped && (
        <UnstyledButton
          aria-expanded={false}
          onClick={() => setExpanded(true)}
          style={{
            display: "flex",
            width: "100%",
            alignItems: "center",
            justifyContent: "center",
            marginTop: "var(--mantine-spacing-xs)",
            padding: "var(--mantine-spacing-xs)",
            borderTop: "1px solid var(--mantine-color-default-border)",
            color: "var(--mantine-color-dimmed)",
            fontSize: "var(--mantine-font-size-xs)",
          }}
        >
          {expandLabel ?? (lines !== undefined && lines > 1 ? `Show all ${lines} lines` : "Show all")}
        </UnstyledButton>
      )}
    </>
  );

  return (
    <div className="ui-clamped-block" data-expanded={expanded} data-label={label}>
      {stickyCollapse ? (
        <Disclosure
          className="ui-clamped-disclosure"
          summary={header ?? label}
          headingVisible={expanded}
          open={true}
          onOpenChange={setExpanded}
        >
          <DisclosureBody>
            {!expanded && header}
            {body}
          </DisclosureBody>
        </Disclosure>
      ) : (
        <>
          <DisclosureBody>
            {header}
            {body}
          </DisclosureBody>
          {expanded && (
            <UnstyledButton
              className="ui-clamped-collapse"
              aria-label={`Show less ${label}`}
              onClick={() => setExpanded(false)}
            >
              Show less
            </UnstyledButton>
          )}
        </>
      )}
    </div>
  );
}
