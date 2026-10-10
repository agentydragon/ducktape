import type { JSX } from "react";
import { z } from "zod";

import { Chip } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

import { resource } from "./resource_common";

const resourcesDeleteArguments = z.strictObject({
  ...resource,
  gracePeriodSeconds: z.int().nonnegative().optional(),
});

export function canQuickApproveResourcesDelete(args: unknown): boolean {
  return resourcesDeleteArguments.safeParse(args).success;
}

function Delete({ args }: { args: z.infer<typeof resourcesDeleteArguments> }): JSX.Element {
  return (
    <>
      <Chip label="API version" value={args.apiVersion} />
      <Chip
        label="grace period"
        value={args.gracePeriodSeconds === undefined ? "default" : `${args.gracePeriodSeconds}s`}
      />
    </>
  );
}

export const resourcesDeletePane: ArgumentsPreview = definePreview(resourcesDeleteArguments, Delete);
