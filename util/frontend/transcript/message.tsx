import { Group, Paper, Stack, Text } from "@mantine/core";
import { type HTMLAttributes, type JSX, type ReactNode } from "react";

import "./message.css";

type UserMessageAttributes = HTMLAttributes<HTMLDivElement> & {
  [key: `data-${string}`]: string | undefined;
};

/** A right-aligned message with optional context in its gutter and an in-bubble overlay. */
export function UserMessage({
  children,
  aside,
  overlay,
  pending = false,
  rowAttributes,
  bubbleAttributes,
}: {
  children?: ReactNode;
  aside?: ReactNode;
  overlay?: ReactNode;
  pending?: boolean;
  rowAttributes?: UserMessageAttributes;
  bubbleAttributes?: UserMessageAttributes;
}): JSX.Element {
  const { className: rowClassName, style: rowStyle, ...rowProps } = rowAttributes ?? {};
  const { className: bubbleClassName, style: bubbleStyle, ...bubbleProps } = bubbleAttributes ?? {};

  return (
    <Group
      {...rowProps}
      className={`transcript-user-message-row${rowClassName ? ` ${rowClassName}` : ""}`}
      justify="flex-end"
      align="flex-start"
      gap="xs"
      wrap="nowrap"
      style={{ width: "100%", ...rowStyle }}
    >
      {aside && (
        <Stack className="transcript-user-message-aside" gap={2} align="flex-end">
          {aside}
        </Stack>
      )}
      <Paper
        {...bubbleProps}
        className={`transcript-user-bubble${bubbleClassName ? ` ${bubbleClassName}` : ""}`}
        p="sm"
        style={{ ...bubbleStyle, ...(pending ? { fontStyle: "italic", opacity: 0.6 } : {}) }}
      >
        {overlay}
        {children}
      </Paper>
    </Group>
  );
}

/** Text kept verbatim, with line breaks preserved and long lines wrapped inside the message. */
export function VerbatimText({ text }: { text: string }): JSX.Element {
  return (
    <Text className="transcript-verbatim" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
      {text}
    </Text>
  );
}
