import { Accordion } from "@mantine/core";
import {
  createContext,
  type CSSProperties,
  type JSX,
  type ReactNode,
  useCallback,
  useContext,
  useLayoutEffect,
  useState,
} from "react";

import "./disclosure.css";

interface StickyStackPosition {
  top: number;
  depth: number;
  /** Higher rows paint over descendants as they leave their containing disclosure. */
  zIndex: number;
}

export const DISCLOSURE_STICKY_Z_INDEX = 100;

const StickyStackContext = createContext<StickyStackPosition>({
  top: 0,
  depth: 0,
  zIndex: DISCLOSURE_STICKY_Z_INDEX,
});

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
  return {
    top: parent.top + rowHeight,
    depth: parent.depth + 1,
    zIndex: parent.zIndex - 1,
  };
}

/** Terminal content owns its gutters; a panel containing nested disclosures leaves their
 * frames full-width. Depth indents labels and reading content, never the surrounding frame. */
export function DisclosureBody({ children }: { children: ReactNode }): JSX.Element {
  const { depth } = useContext(StickyStackContext);
  return (
    <div className="agentplane-disclosure-body" style={disclosureIndent(depth)}>
      {children}
    </div>
  );
}

function disclosureIndent(depth: number): CSSProperties {
  return { "--agentplane-disclosure-depth": depth } as CSSProperties;
}

/** A non-interactive step shares the disclosure row's typography, spacing and nesting inset. */
export function DisclosureRow({ children, className }: { children: ReactNode; className: string }): JSX.Element {
  const { depth } = useContext(StickyStackContext);
  return (
    <div className={`agentplane-disclosure-static ${className}`} style={disclosureIndent(depth)}>
      {children}
    </div>
  );
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
      style={disclosureIndent(stackPosition.depth)}
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
