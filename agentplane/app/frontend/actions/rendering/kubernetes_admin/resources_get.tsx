import type { JSX } from "react";
import { z } from "zod";

import { CompactCall } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

import { resource, ResourceChips } from "./resource_common";

const resourcesGetArguments = z.strictObject({ ...resource, namespace: z.string().min(1).optional() });

export function canQuickApproveResourcesGet(args: unknown): boolean {
  return resourcesGetArguments.safeParse(args).success;
}
function Get({ args }: { args: z.infer<typeof resourcesGetArguments> }): JSX.Element {
  return (
    <CompactCall operation="Get resource">
      <ResourceChips args={args} />
    </CompactCall>
  );
}

export const resourcesGetPreview: ArgumentsPreview = definePreview(resourcesGetArguments, Get);
