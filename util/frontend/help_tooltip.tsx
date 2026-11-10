import { ActionIcon, Tooltip } from "@mantine/core";
import { type JSX } from "react";
import { useTooltipInteraction } from "./tooltip_interaction";

/** A compact, keyboard-accessible question mark for explanatory copy beside a label. */
export function HelpTooltip({ topic, description }: { topic: string; description: string }): JSX.Element {
  const { opened, triggerProps } = useTooltipInteraction<HTMLButtonElement>();

  return (
    <Tooltip
      opened={opened}
      label={description}
      events={{ hover: false, focus: false, touch: false }}
      multiline
      w="min(320px, calc(100vw - 16px))"
      withArrow
      position="top"
    >
      <ActionIcon
        {...triggerProps}
        size="xs"
        variant="subtle"
        color="gray"
        style={{ color: "inherit" }}
        aria-label={`About ${topic}`}
      >
        <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" focusable="false">
          <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path
            d="M6.4 6a1.7 1.7 0 1 1 3.1 1c-.6.8-1.5.9-1.5 2.1"
            fill="none"
            stroke="currentColor"
            strokeLinecap="round"
            strokeWidth="1.4"
          />
          <circle cx="8" cy="11.6" r="0.7" fill="currentColor" />
        </svg>
      </ActionIcon>
    </Tooltip>
  );
}
