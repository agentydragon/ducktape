import { Text } from "@mantine/core";
import type { JSX, ReactNode, Ref } from "react";

import { Disclosure } from "../disclosure";

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
  /** Controls that change the disclosed content, kept outside the sticky summary. */
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
  const openControls = expandable && open && controls && <div className="agentplane-step-controls">{controls}</div>;
  return (
    <div className="agentplane-step-row">
      {expandable ? (
        <Disclosure
          className="agentplane-step-details"
          dividerBoundary
          open={open}
          onOpenChange={onOpenChange}
          summary={summary}
        >
          {openControls}
          {children}
        </Disclosure>
      ) : (
        <div className="agentplane-step-static">{summary}</div>
      )}
      {aside}
    </div>
  );
}
