import { Accordion, Button } from "@mantine/core";
import { createContext, type JSX, type ReactNode, useContext, useState } from "react";

import "./disclosure.css";

// Nested sticky rows share one top slot. Deeper disclosures and their collapse controls paint over
// ancestors instead of stacking another toolbar below each parent heading.
const DisclosureDepth = createContext(0);

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
  const depth = useContext(DisclosureDepth);
  return (
    <div
      className={`agentplane-disclosure-collapse${className ? ` ${className}` : ""}`}
      data-expanded={expanded}
      data-label={label}
      style={{
        top: 0,
        zIndex: 4 + depth,
        // Cover the ancestor summary across its full width when this deeper action occupies the
        // shared sticky slot; account for each panel inset and the enclosing Mantine card padding.
        marginInline: depth > 0 ? `calc(var(--mantine-spacing-md) * -${depth} - var(--mantine-spacing-sm))` : undefined,
      }}
    >
      <div className="agentplane-disclosure-collapse-label">{header ?? label}</div>
      {expanded && (
        <Button
          variant="subtle"
          size="sm"
          className="agentplane-disclosure-collapse-button"
          aria-expanded={true}
          aria-label={`Collapse ${label}`}
          onClick={onCollapse}
        >
          Collapse {label}
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
  const depth = useContext(DisclosureDepth);
  const [localOpen, setLocalOpen] = useState(defaultOpen);
  const expanded = open ?? localOpen;
  const setExpanded = (next: boolean) => {
    if (open === undefined) setLocalOpen(next);
    onOpenChange?.(next);
  };

  return (
    <DisclosureDepth.Provider value={depth + 1}>
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
            className="agentplane-disclosure-heading"
            data-expanded={expanded}
            data-depth={depth}
            style={{ top: 0, zIndex: 2 + depth + (expanded ? 1 : 0) }}
          >
            <Accordion.Control>{summary}</Accordion.Control>
            {summaryAside}
          </div>
          <Accordion.Panel>{(expanded || keepMounted) && children}</Accordion.Panel>
        </Accordion.Item>
      </Accordion>
    </DisclosureDepth.Provider>
  );
}
