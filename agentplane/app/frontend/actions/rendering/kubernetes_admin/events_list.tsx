import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";

const eventsListArguments = z.strictObject({
  namespace: z.string().min(1).optional(),
  fieldSelector: z.string().min(1).optional(),
});

export function offersCardDecisionButtonsEventsList(args: unknown): boolean {
  return eventsListArguments.safeParse(args).success;
}

function Events({ args }: { args: z.infer<typeof eventsListArguments> }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      {args.fieldSelector ? (
        <>
          Show events matching <Code>{args.fieldSelector}</Code>.
        </>
      ) : (
        "Show all events."
      )}
    </Text>
  );
}

function EventsLabel({ args }: { args: z.infer<typeof eventsListArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      List events
      {args.namespace && (
        <>
          {" "}
          in namespace <Code>{args.namespace}</Code>
        </>
      )}
    </Text>
  );
}

export const eventsListLabel: ArgumentsPreview = definePreview(eventsListArguments, EventsLabel);
export const eventsListPane: ArgumentsPreview = definePreview(eventsListArguments, Events);

export const eventsListPresentation: ActionPresentation = {
  label: eventsListLabel,
  pane: { collapsed: eventsListPane, opened: eventsListPane },
  details: { arguments: eventsListPane },
  offersCardDecisionButtons: offersCardDecisionButtonsEventsList,
};
