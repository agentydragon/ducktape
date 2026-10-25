import { ActionIcon, Tooltip } from "@mantine/core";
import type { JSX } from "react";

import { IconHelpCircle } from "./icons";

/** A compact, keyboard-accessible question mark for explanatory copy beside a label. */
export function HelpTooltip({ topic, description }: { topic: string; description: string }): JSX.Element {
  return (
    <Tooltip label={description} multiline w={320} withArrow events={{ hover: true, focus: true, touch: true }}>
      <ActionIcon size="xs" variant="subtle" color="gray" aria-label={`About ${topic}`} title={description}>
        <IconHelpCircle size={15} />
      </ActionIcon>
    </Tooltip>
  );
}
