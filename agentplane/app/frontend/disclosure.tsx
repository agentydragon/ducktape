import { Accordion, Button } from "@mantine/core";
import { createContext, type JSX, type ReactNode, useContext, useLayoutEffect, useRef, useState } from "react";

import "./disclosure.css";

const DisclosureStickyOffset = createContext(0);

/** A sticky close affordance for expanded text that must stay mounted while clipped, such as a
 * command or tool output. */
export function StickyCollapseControl({
  label,
  header,
  expanded,
  onCollapse,
  className,
}: {
  label: string;
  header?: ReactNode;
  expanded: boolean;
  onCollapse: () => void;
  className?: string;
}): JSX.Element {
  const top = useContext(DisclosureStickyOffset);
  return (
    <div
      className={`agentplane-disclosure-collapse${className ? ` ${className}` : ""}`}
      data-expanded={expanded}
      style={{ top }}
    >
      <div className="agentplane-disclosure-collapse-label">{header ?? label}</div>
      {expanded && (
        <Button
          variant="subtle"
          size="compact-xs"
          aria-expanded={true}
          aria-label={`Collapse ${label}`}
          onClick={onCollapse}
        >
          Collapse
        </Button>
      )}
    </div>
  );
}

/** A shared Mantine Accordion with optional controlled state. Its controls keep Mantine's styles,
 * keyboard behavior, and chevron; the wrapper adds only sticky positioning and nested offsets. */
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
  const parentTop = useContext(DisclosureStickyOffset);
  const [localOpen, setLocalOpen] = useState(defaultOpen);
  const [headerHeight, setHeaderHeight] = useState(0);
  const headingRef = useRef<HTMLDivElement>(null);
  const expanded = open ?? localOpen;
  const setExpanded = (next: boolean) => {
    if (open === undefined) setLocalOpen(next);
    onOpenChange?.(next);
  };

  useLayoutEffect(() => {
    const heading = headingRef.current;
    if (!heading) return;
    const measure = () => {
      const nextHeight = heading.getBoundingClientRect().height;
      setHeaderHeight((current) => (Math.abs(current - nextHeight) < 0.5 ? current : nextHeight));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(heading);
    return () => observer.disconnect();
  }, []);

  return (
    <DisclosureStickyOffset.Provider value={parentTop + headerHeight}>
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
            style={{ top: parentTop }}
          >
            <Accordion.Control>{summary}</Accordion.Control>
            {summaryAside}
          </div>
          <Accordion.Panel>{(expanded || keepMounted) && children}</Accordion.Panel>
        </Accordion.Item>
      </Accordion>
    </DisclosureStickyOffset.Provider>
  );
}
