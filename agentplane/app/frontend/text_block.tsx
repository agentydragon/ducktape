import { type JSX, useMemo } from "react";

import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "../../../util/frontend/clamped_block";
import { CodeBlock, type Language } from "../../../util/frontend/code_block";
import { OperationSection } from "./operation_section";

/** Selectable text with optional syntax, a shared surface, and expansion for long content. */
export function TextBlock({
  text,
  language,
  heading,
  format = "code",
  clamp = true,
}: {
  text: string;
  language?: Language;
  /** A meaningful section name shown while collapsed and pinned while expanded. */
  heading?: string;
  /** Prose uses proportional type; payloads default to monospace even without a known grammar. */
  format?: "code" | "prose";
  /** Fold long content by default; callers can opt out when the full text should be visible. */
  clamp?: boolean;
}): JSX.Element {
  const content = (
    <CodeBlock
      text={text}
      language={format === "code" ? language : undefined}
      surface="plain"
      font={format === "prose" ? "prose" : "monospace"}
      releaseWhenFar
    />
  );
  const header = heading && (
    <strong className="agentplane-operation-heading" data-operation-heading={heading}>
      {heading}
    </strong>
  );
  return (
    <OperationSection surface={format === "prose" ? "plain" : "framed"}>
      {clamp ? (
        <ClampedBlock
          maxHeightRem={TEXT_MAX_HEIGHT_REM}
          lines={language === "yaml" ? lineCount(text) : undefined}
          label={heading ?? (format === "prose" ? "Text" : "Payload")}
          expandLabel={language === "yaml" ? undefined : "Show more"}
          header={header}
        >
          {content}
        </ClampedBlock>
      ) : (
        <>
          {header}
          {content}
        </>
      )}
    </OperationSection>
  );
}

export function JsonBlock({
  value,
  heading,
  clamp = true,
}: {
  value: unknown;
  heading?: string;
  clamp?: boolean;
}): JSX.Element {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  return <TextBlock text={text} language="json" heading={heading} clamp={clamp} />;
}
