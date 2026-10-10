import { Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { Chip } from "./chips";
import { definePreview, type ArgumentsPreview } from "./entry";

const calendarNodeArguments = z.strictObject({
  workspaceId: z.string(),
  granularity: z.enum(["day", "week", "month", "year"]),
  date: z.string().optional(),
});

function CalendarNodeArguments({ args }: { args: z.infer<typeof calendarNodeArguments> }): JSX.Element {
  return (
    <Stack gap={4}>
      <Chip label="Granularity" value={args.granularity} />
      {args.date && <Chip label="Date" value={args.date} />}
      <Chip label="Workspace ID" value={args.workspaceId} />
    </Stack>
  );
}

function CalendarNodeLabel({ args }: { args: z.infer<typeof calendarNodeArguments> }): JSX.Element {
  void args;
  return (
    <Text size="sm" fw={600}>
      Get or create Tana calendar node
    </Text>
  );
}

export const calendarNodeLabel: ArgumentsPreview = definePreview(calendarNodeArguments, CalendarNodeLabel);
export const calendarNodeArgumentsPreview: ArgumentsPreview = definePreview(
  calendarNodeArguments,
  CalendarNodeArguments
);
