import { Text } from "@mantine/core";
import type { JSX, ReactNode, Ref } from "react";

/** How a step stands, as its title shows it while the step is folded. A streaming step breathes; an
 * incomplete one, which nothing is working on, does not. */
export type StepMark = "failed" | "streaming" | "incomplete";

const MARKS: Record<StepMark, { color: string; label: string }> = {
  failed: { color: "red", label: "Failed" },
  streaming: { color: "blue", label: "Streaming" },
  incomplete: { color: "blue", label: "Incomplete" },
};

/** One step of a run (a reasoning step, a tool call) as a single line: a title, a one-line preview
 * of what is behind it, and, when there is something to disclose, a disclosure that opens it below.
 * The preview gives way to the content once it is open, as the content then says it in full.
 *
 * Styled by `.agentplane-step-*` in projected_session.css. */
export function StepLine({
  title,
  preview,
  previewRef,
  mark,
  trailing,
  aside,
  controls,
  expandable,
  open,
  onOpenChange,
  children,
}: {
  title: string;
  preview: ReactNode;
  previewRef?: Ref<HTMLDivElement>;
  mark?: StepMark;
  /** Kept on the line when open, after the preview: a step's status. */
  trailing?: ReactNode;
  /** Beside the line, outside its disclosure. */
  aside?: ReactNode;
  /** Controls for the disclosed content, at the end of the line while it is open. Outside the
   * summary, so using one does not toggle the disclosure. */
  controls?: ReactNode;
  expandable: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The disclosed content, mounted only while open. */
  children?: ReactNode;
}): JSX.Element {
  const summary = (
    <div className="agentplane-step-summary">
      <Text
        component="span"
        className={`agentplane-step-title${mark === "streaming" ? " agentplane-step-title--streaming" : ""}`}
        c={mark ? MARKS[mark].color : "dimmed"}
        title={mark && MARKS[mark].label}
      >
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
      {expandable && open && controls && <div className="agentplane-step-controls">{controls}</div>}
    </div>
  );
}
