import type { JSX } from "react";
import { z } from "zod";

import { Chip, CompactCall } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

const logs = z.strictObject({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  container: z.string().min(1).optional(),
  previous: z.boolean().optional(),
  tail: z.int().optional(),
});

function Logs({ args }: { args: z.infer<typeof logs> }): JSX.Element {
  return (
    <CompactCall operation="Get pod logs">
      <Chip label="pod" value={args.name} />
      <Chip label="namespace" value={args.namespace ?? "(not specified)"} />
      <Chip label="container" value={args.container ?? "(not specified)"} />
      <Chip label="previous" value={args.previous === true ? "yes" : "no"} />
      <Chip label="tail" value={args.tail ?? 100} />
    </CompactCall>
  );
}

export const podsLogPreview: ArgumentsPreview = definePreview(logs, Logs);
export const podsLogCompact: ArgumentsPreview = podsLogPreview;
