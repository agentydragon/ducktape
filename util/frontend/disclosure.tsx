import { Accordion, Paper } from "@mantine/core";
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

function useMeasuredHeadingHeight(visible: boolean) {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const [height, setHeight] = useState(0);
  const ref = useCallback((node: HTMLDivElement | null) => setElement(node), []);

  useLayoutEffect(() => {
    if (!element || !visible) {
      setHeight(0);
      return;
    }

    const measure = () => {
      setHeight(element.getBoundingClientRect().height);
    };

    // Seed before paint so a nested heading does not briefly share its parent's sticky slot.
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [element, visible]);

  return [ref, height] as const;
}

function childStackPosition(parent: StickyStackPosition, rowHeight: number): StickyStackPosition {
  return {
    top: parent.top + rowHeight,
    depth: parent.depth + 1,
    zIndex: parent.zIndex - 1,
  };
}

const DisclosureCardContext = createContext(false);

/** One frame around a group; nested cards keep the same DOM without drawing another frame. */
export function DisclosureCard({
  open,
  children,
  className,
}: {
  open: boolean;
  children: ReactNode;
  className?: string;
}): JSX.Element {
  const nested = useContext(DisclosureCardContext);
  return (
    <DisclosureCardContext.Provider value={true}>
      <Paper
        className={`agentplane-collapsible-card${className ? ` ${className}` : ""}`}
        data-open={open}
        data-nested={nested}
      >
        {children}
      </Paper>
    </DisclosureCardContext.Provider>
  );
}

/** Terminal content inherits the inset computed by its owning section, exactly like its label. */
export function DisclosureBody({ children }: { children: ReactNode }): JSX.Element {
  return <div className="agentplane-disclosure-body">{children}</div>;
}

function DisclosureAside({ children }: { children?: ReactNode }): JSX.Element | null {
  return children ? <div className="agentplane-disclosure-aside">{children}</div> : null;
}

function disclosureIndent(depth: number): CSSProperties {
  return { "--agentplane-disclosure-depth": depth } as CSSProperties;
}

/** A non-interactive step shares the disclosure row's typography, spacing and nesting inset. */
export function DisclosureRow({
  children,
  className,
  summaryAside,
}: {
  children: ReactNode;
  className: string;
  summaryAside?: ReactNode;
}): JSX.Element {
  const { depth } = useContext(StickyStackContext);
  return (
    <div
      className={`agentplane-disclosure-header agentplane-disclosure-static ${className}`}
      style={disclosureIndent(depth)}
    >
      <div className="agentplane-disclosure-static-content">{children}</div>
      <DisclosureAside>{summaryAside}</DisclosureAside>
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
  headingVisible = true,
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
  /** A clipped preview retains its content/section while its expanded heading is hidden. */
  headingVisible?: boolean;
}): JSX.Element {
  const stackPosition = useContext(StickyStackContext);
  const [headingRef, headingHeight] = useMeasuredHeadingHeight(headingVisible);
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
      chevronSize="var(--agentplane-disclosure-chevron-size)"
      keepMounted={keepMounted}
      keepMountedMode={keepMounted ? "display-none" : undefined}
      value={expanded ? "content" : null}
      onChange={(value) => setExpanded(value === "content")}
      classNames={{
        item: "agentplane-disclosure-item",
        control: "agentplane-disclosure-summary",
        label: "agentplane-disclosure-summary-content",
        chevron: "agentplane-disclosure-chevron",
        panel: "agentplane-disclosure-panel",
        content: "agentplane-disclosure-content",
      }}
    >
      <Accordion.Item value="content" {...dataAttributes}>
        <div
          ref={headingRef}
          className="agentplane-disclosure-header agentplane-disclosure-heading"
          data-expanded={expanded}
          data-depth={stackPosition.depth}
          hidden={!headingVisible}
          style={{ top: stackPosition.top, zIndex: stackPosition.zIndex }}
        >
          <Accordion.Control>{summary}</Accordion.Control>
          <DisclosureAside>{summaryAside}</DisclosureAside>
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
