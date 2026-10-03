import { Text } from "@mantine/core";
import type { JSX, ReactNode, Ref } from "react";

/** One step of a run (a reasoning step, a tool call) as a single line: a title, a one-line preview
 * of what is behind it, and, when there is something to disclose, a disclosure that opens it below.
 * The preview gives way to the content once it is open, as the content then says it in full.
 *
 * Styled by `.agentplane-step-*` in projected_session.css. */
export function StepLine({
  title,
  preview,
  previewRef,
  trailing,
  aside,
  expandable,
  open,
  onOpenChange,
  children,
}: {
  title: string;
  preview: ReactNode;
  previewRef?: Ref<HTMLDivElement>;
  /** Kept on the line when open, after the preview: a step's status. */
  trailing?: ReactNode;
  /** Beside the line, outside its disclosure. */
  aside?: ReactNode;
  expandable: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The disclosed content, mounted only while open. */
  children?: ReactNode;
}): JSX.Element {
  const summary = (
    <div className="agentplane-step-summary">
      <Text component="span" className="agentplane-step-title" c="dimmed">
        {title}
      </Text>
      <div className="agentplane-step-preview" ref={previewRef}>
        {preview}
      </div>
      {trailing && <span className="agentplane-step-trailing">{trailing}</span>}
    </div>
  );
  return (
    <div className="agentplane-step-row">
      {expandable ? (
        <details
          className="agentplane-step-details"
          open={open}
          onToggle={(event) => onOpenChange(event.currentTarget.open)}
        >
          <summary>{summary}</summary>
          {open && children}
        </details>
      ) : (
        <div className="agentplane-step-static">{summary}</div>
      )}
      {aside}
    </div>
  );
}
