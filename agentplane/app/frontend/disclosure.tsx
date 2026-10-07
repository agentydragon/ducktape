import { Accordion } from "@mantine/core";
import { createContext, type JSX, type ReactNode, useCallback, useContext, useLayoutEffect, useState } from "react";

import "./disclosure.css";

interface StickyStackPosition {
  top: number;
  /** Higher rows paint over descendants as they leave their containing disclosure. */
  zIndex: number;
}

const StickyStackContext = createContext<StickyStackPosition>({ top: 0, zIndex: 100 });

function useStickyRowHeight() {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const [height, setHeight] = useState(0);
  const ref = useCallback((node: HTMLDivElement | null) => setElement(node), []);

  useLayoutEffect(() => {
    if (!element) return;

    const measure = () => {
      const nextHeight = element.getBoundingClientRect().height;
      setHeight((current) => (Math.abs(current - nextHeight) < 0.5 ? current : nextHeight));
    };

    // Seed before paint so a nested heading does not briefly share its parent's sticky slot.
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);

  return [ref, height] as const;
}

function childStackPosition(parent: StickyStackPosition, rowHeight: number): StickyStackPosition {
  return { top: parent.top + rowHeight, zIndex: parent.zIndex - 1 };
}

/** A shared Mantine Accordion with optional controlled state. Its controls keep Mantine's styles,
 * keyboard behavior, and chevron; the wrapper coordinates sticky rows through nested content. */
export function Disclosure({
  summary,
  summaryAside,
  children,
  open,
  defaultOpen = false,
  onOpenChange,
  className,
  dataAttributes,
  keepMounted = false,
}: {
  summary: ReactNode;
  /** A separate header action, outside the accordion button. */
  summaryAside?: ReactNode;
  children: ReactNode;
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
  dataAttributes?: Record<string, string>;
  /** Keep hidden content mounted when the caller relies on native selection/copy behavior. */
  keepMounted?: boolean;
}): JSX.Element {
  const stackPosition = useContext(StickyStackContext);
  const [headingRef, headingHeight] = useStickyRowHeight();
  const [localOpen, setLocalOpen] = useState(defaultOpen);
  const expanded = open ?? localOpen;
  const setExpanded = (next: boolean) => {
    if (open === undefined) setLocalOpen(next);
    onOpenChange?.(next);
  };

  return (
    <Accordion
      className={`agentplane-disclosure${className ? ` ${className}` : ""}`}
      transitionDuration={0}
      keepMounted={keepMounted}
      keepMountedMode={keepMounted ? "display-none" : undefined}
      value={expanded ? "content" : null}
      onChange={(value) => setExpanded(value === "content")}
      classNames={{
        item: "agentplane-disclosure-item",
        control: "agentplane-disclosure-summary",
        label: "agentplane-disclosure-summary-content",
        panel: "agentplane-disclosure-panel",
        content: "agentplane-disclosure-content",
      }}
    >
      <Accordion.Item value="content" {...dataAttributes}>
        <div
          ref={headingRef}
          className="agentplane-disclosure-heading"
          data-expanded={expanded}
          style={{ top: stackPosition.top, zIndex: stackPosition.zIndex }}
        >
          <Accordion.Control>{summary}</Accordion.Control>
          {summaryAside}
        </div>
        <Accordion.Panel>
          <StickyStackContext.Provider value={childStackPosition(stackPosition, headingHeight)}>
            {(expanded || keepMounted) && children}
          </StickyStackContext.Provider>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
