import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import { Chip } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

const eventsListArguments = z.strictObject({
  namespace: z.string().min(1).optional(),
  fieldSelector: z.string().min(1).optional(),
});

export function canQuickApproveEventsList(args: unknown): boolean {
  return eventsListArguments.safeParse(args).success;
}

function Events({ args }: { args: z.infer<typeof eventsListArguments> }): JSX.Element {
  return <Chip label="field selector" value={args.fieldSelector ?? "all events"} />;
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
