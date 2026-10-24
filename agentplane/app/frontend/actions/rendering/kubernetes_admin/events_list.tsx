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

function Events({ fieldSelector }: { fieldSelector: string }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Show events matching <Code>{fieldSelector}</Code>.
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
// Without a field selector the label is the whole call; direct previews preserve null as the slot
// suppression signal after schema validation.
export const eventsListPane: ArgumentsPreview = {
  schema: eventsListArguments,
  render: (args: z.infer<typeof eventsListArguments>) =>
    args.fieldSelector === undefined ? null : <Events fieldSelector={args.fieldSelector} />,
};

export const eventsListPresentation: ActionPresentation = {
  label: eventsListLabel,
  pane: { collapsed: eventsListPane, opened: eventsListPane },
  details: { arguments: eventsListPane },
  offersCardDecisionButtons: offersCardDecisionButtonsEventsList,
};
