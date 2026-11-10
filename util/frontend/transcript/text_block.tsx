import { Text } from "@mantine/core";
import { type JSX } from "react";

import { ClampedBlock, lineCount, TEXT_MAX_HEIGHT_REM } from "../clamped_block";
import { CodeBlock, type Language } from "../code_block";

/** True when text starts with a JSON object or array. */
export function looksLikeJson(text: string): boolean {
  return /^\s*[[{]/.test(text);
}

/** Labeled, syntax-highlighted text with a bounded height and retained optional expansion. */
export function TextBlock({
  label,
  text,
  language,
  expansion,
  maxHeightRem = TEXT_MAX_HEIGHT_REM,
}: {
  label: string;
  text: string;
  language?: Language;
  expansion?: readonly [boolean, (expanded: boolean) => void];
  maxHeightRem?: number;
}): JSX.Element {
  return (
    <ClampedBlock
      maxHeightRem={maxHeightRem}
      lines={lineCount(text)}
      label={label}
      header={
        <Text size="sm" c="dimmed">
          {label}
        </Text>
      }
      expansion={expansion}
    >
      <CodeBlock text={text} language={language ?? (looksLikeJson(text) ? "json" : undefined)} surface="plain" />
    </ClampedBlock>
  );
}
