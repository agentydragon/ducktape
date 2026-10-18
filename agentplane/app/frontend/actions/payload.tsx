import { type JSX, useMemo } from "react";

import { ClampedBlock, lineCount } from "../clamped_block";
import { CodeBlock, type Language } from "../code_block";
import { OUTPUT_MAX_HEIGHT_REM } from "../command_view";

export function ActionText({ text, language }: { text: string; language?: Language }): JSX.Element {
  return (
    <ClampedBlock maxHeightRem={OUTPUT_MAX_HEIGHT_REM} lines={lineCount(text)} label="Action text">
      <CodeBlock text={text} language={language} />
    </ClampedBlock>
  );
}

export function ActionJson({ value }: { value: unknown }): JSX.Element {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  return <ActionText text={text} language="json" />;
}
