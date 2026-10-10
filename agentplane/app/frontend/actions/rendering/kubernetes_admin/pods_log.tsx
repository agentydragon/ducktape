import type { JSX } from "react";
import { z } from "zod";

import { Chip } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

const podsLogArguments = z.strictObject({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  container: z.string().min(1).optional(),
  previous: z.boolean().optional(),
  tail: z.int().optional(),
});

export function canQuickApprovePodsLog(args: unknown): boolean {
  return podsLogArguments.safeParse(args).success;
}

function Logs({ args }: { args: z.infer<typeof podsLogArguments> }): JSX.Element {
  return (
    <>
      <Chip label="container" value={args.container ?? "(default)"} />
      <Chip label="previous" value={args.previous === true ? "yes" : "no"} />
      <Chip label="tail" value={args.tail ?? 100} />
    </>
  );
}

export const podsLogPreview: ArgumentsPreview = definePreview(podsLogArguments, Logs);
