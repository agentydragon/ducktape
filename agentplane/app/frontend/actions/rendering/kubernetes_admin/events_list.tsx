import type { JSX } from "react";
import { z } from "zod";

import { Chip, CompactCall } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

const eventsListArguments = z.strictObject({
  namespace: z.string().min(1).optional(),
  fieldSelector: z.string().min(1).optional(),
});

export function canQuickApproveEventsList(args: unknown): boolean {
  return eventsListArguments.safeParse(args).success;
}

function Events({ args }: { args: z.infer<typeof eventsListArguments> }): JSX.Element {
  return (
    <CompactCall operation="List events">
      <Chip label="namespace" value={args.namespace ?? "all namespaces"} />
      {args.fieldSelector !== undefined && <Chip label="field selector" value={args.fieldSelector} />}
    </CompactCall>
  );
}

export const eventsListPane: ArgumentsPreview = definePreview(eventsListArguments, Events);
