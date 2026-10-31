import { Text } from "@mantine/core";
import type { JSX, ReactNode, Ref } from "react";

import { Disclosure, DisclosureBody, DisclosureRow } from "../disclosure";

import "./step_line.css";

/** How a step stands, as its title shows it while the step is folded. A streaming step breathes; an
 * incomplete one, which nothing is working on, does not. */
export type StepMark = "failed" | "streaming" | "incomplete";

const MARKS: Record<StepMark, { color: string; label: string }> = {
  failed: { color: "red", label: "Failed" },
  streaming: { color: "blue", label: "Streaming" },
  incomplete: { color: "blue", label: "Incomplete" },
};

/** One step as a compact line with a one-line preview and optional disclosed content below. */
export function StepLine({
  title,
  preview,
  previewRef,
  mark,
  trailing,
  aside,
  summaryAside,
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
  /** Kept on the line when open, after the preview. */
  trailing?: ReactNode;
  /** Beside the line, outside its disclosure. */
  aside?: ReactNode;
  /** An action beside the preview, outside the disclosure button. */
  summaryAside?: ReactNode;
  /** Controls that change the disclosed content, kept outside the sticky summary. */
  controls?: ReactNode;
  expandable: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The disclosed content, mounted only while open. */
  children?: ReactNode;
}): JSX.Element {
  const summary = (
    <div className="transcript-step-summary">
      <Text
        component="span"
        size="sm"
        className={`transcript-step-title${mark === "streaming" ? " transcript-step-title--streaming" : ""}`}
        c={mark ? MARKS[mark].color : "dimmed"}
        title={mark && MARKS[mark].label}
      >
        {title}
      </Text>
      <div className="transcript-step-preview" ref={previewRef}>
        {preview}
      </div>
      {trailing && <span className="transcript-step-trailing">{trailing}</span>}
    </div>
  );
  const openControls = expandable && open && controls && (
    <DisclosureBody>
      <div className="transcript-step-controls">{controls}</div>
    </DisclosureBody>
  );
  return (
    <div className="transcript-step-row">
      {expandable ? (
        <Disclosure
          className="transcript-step-details"
          open={open}
          onOpenChange={onOpenChange}
          summary={summary}
          summaryAside={summaryAside && <span className="transcript-step-inspect">{summaryAside}</span>}
        >
          {openControls}
          {children}
        </Disclosure>
      ) : (
        <DisclosureRow
          className="transcript-step-static"
          summaryAside={summaryAside && <span className="transcript-step-inspect">{summaryAside}</span>}
        >
          {summary}
        </DisclosureRow>
      )}
      {aside}
    </div>
  );
}
