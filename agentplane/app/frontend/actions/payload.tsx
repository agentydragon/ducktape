import { Stack, Text } from "@mantine/core";
import { type JSX, useMemo } from "react";

import { ClampedBlock, lineCount } from "../clamped_block";
import { CodeBlock, type Language } from "../code_block";
import { OUTPUT_MAX_HEIGHT_REM } from "../command_view";

export function ActionText({
  text,
  language,
  heading,
  clamp = true,
}: {
  text: string;
  language?: Language;
  heading?: string;
  clamp?: boolean;
}): JSX.Element {
  const label = heading ?? "Action text";
  const block = <CodeBlock text={text} language={language} />;
  return (
    <Stack gap={4}>
      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }} data-operation-heading={heading}>
        {label}
      </Text>
      {clamp ? (
        <ClampedBlock maxHeightRem={OUTPUT_MAX_HEIGHT_REM} lines={lineCount(text)} label={label}>
          {block}
        </ClampedBlock>
      ) : (
        block
      )}
    </Stack>
  );
}

export function ActionJson({ value, clamp = true }: { value: unknown; clamp?: boolean }): JSX.Element {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  return <ActionText text={text} language="json" clamp={clamp} />;
}
