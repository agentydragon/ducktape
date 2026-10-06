import { Button } from "@mantine/core";
import { createContext, type CSSProperties, type JSX, type ReactNode, useContext, useState } from "react";

import "./disclosure.css";

const DisclosureDepth = createContext(0);

function depthStyle(depth: number): CSSProperties {
  return { top: `${depth * 2.25}rem` };
}

export function useDisclosureDepth(): number {
  return useContext(DisclosureDepth);
}

/** The common summary for expandable content. It stays at the top of the scrollport while the
 * disclosure is open, with nested headers stacked below their open parents. */
export function DisclosureSummary({
  children,
  open,
  depth,
  className,
}: {
  children: ReactNode;
  open: boolean;
  depth: number;
  className?: string;
}): JSX.Element {
  return (
    <summary
      className={`agentplane-disclosure-summary${className ? ` ${className}` : ""}`}
      data-expanded={open}
      style={depthStyle(depth)}
    >
      <span className="agentplane-disclosure-caret" aria-hidden="true" />
      <div className="agentplane-disclosure-summary-content">{children}</div>
    </summary>
  );
}

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
  const depth = useDisclosureDepth();
  return (
    <div
      className={`agentplane-disclosure-collapse${className ? ` ${className}` : ""}`}
      data-expanded={expanded}
      style={depthStyle(depth)}
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

/** Native disclosure behavior with an optional controlled open state. All Agentplane disclosures
 * use this shell so their close affordance, keyboard behavior, and nested sticky positioning match. */
export function Disclosure({
  summary,
  children,
  open,
  defaultOpen = false,
  onOpenChange,
  className,
  dataAttributes,
  keepMounted = false,
}: {
  summary: ReactNode;
  children: ReactNode;
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
  dataAttributes?: Record<string, string>;
  /** Keep hidden content mounted when the caller relies on native selection/copy behavior. */
  keepMounted?: boolean;
}): JSX.Element {
  const depth = useDisclosureDepth();
  const [localOpen, setLocalOpen] = useState(defaultOpen);
  const expanded = open ?? localOpen;
  const setExpanded = (next: boolean) => {
    if (open === undefined) setLocalOpen(next);
    onOpenChange?.(next);
  };

  return (
    <DisclosureDepth.Provider value={depth + 1}>
      <details
        {...dataAttributes}
        className={`agentplane-disclosure${className ? ` ${className}` : ""}`}
        open={expanded}
        onToggle={(event) => setExpanded(event.currentTarget.open)}
      >
        <DisclosureSummary open={expanded} depth={depth}>
          {summary}
        </DisclosureSummary>
        {(expanded || keepMounted) && children}
      </details>
    </DisclosureDepth.Provider>
  );
}
