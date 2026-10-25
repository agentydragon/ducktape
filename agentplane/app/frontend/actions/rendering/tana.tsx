import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { definePreview, type ArgumentsPreview } from "./entry";

const calendarNodeArguments = z.strictObject({
  workspaceId: z.string(),
  granularity: z.enum(["day", "week", "month", "year"]),
  date: z.string().optional(),
});

export function offersCardDecisionButtonsCalendarNode(args: unknown): boolean {
  return calendarNodeArguments.safeParse(args).success;
}

function CalendarNodeArguments({ args }: { args: z.infer<typeof calendarNodeArguments> }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Get or create the <Code>{args.granularity}</Code> calendar node
      {args.date && (
        <>
          {" "}
          for <Code>{args.date}</Code>
        </>
      )}
      {" in workspace "}
      <Code>{args.workspaceId}</Code>.
    </Text>
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

export const calendarNodePresentation: ActionPresentation = {
  label: calendarNodeLabel,
  pane: { collapsed: calendarNodeArgumentsPreview },
  details: { arguments: calendarNodeArgumentsPreview },
  offersCardDecisionButtons: offersCardDecisionButtonsCalendarNode,
  compactCardExpands: expandsForRejectedArguments(calendarNodeArguments),
};

export const tanaRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["get_or_create_calendar_node", calendarNodePresentation],
]);
