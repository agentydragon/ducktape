import type { JSX } from "react";
import { z } from "zod";

import { Chip, CompactCall } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

import { resource } from "./resource_common";

const listSelectors = {
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
};
const resourcesListArguments = z.strictObject({
  apiVersion: resource.apiVersion,
  kind: resource.kind,
  namespace: resource.namespace.optional(),
  ...listSelectors,
});

export function canQuickApproveResourcesList(args: unknown): boolean {
  return resourcesListArguments.safeParse(args).success;
}

function Selectors({ args }: { args: z.infer<typeof resourcesListArguments> }): JSX.Element {
  return (
    <>
      {args.fieldSelector !== undefined && <Chip label="field selector" value={args.fieldSelector} />}
      {args.labelSelector !== undefined && <Chip label="label selector" value={args.labelSelector} />}
    </>
  );
}

function List({ args }: { args: z.infer<typeof resourcesListArguments> }): JSX.Element {
  return (
    <CompactCall operation="List resources">
      <Chip label="API" value={args.apiVersion} />
      <Chip label="kind" value={args.kind} />
      <Chip label="namespace" value={args.namespace ?? "all namespaces / ignored if cluster-scoped"} />
      <Selectors args={args} />
    </CompactCall>
  );
}

export const resourcesListPane: ArgumentsPreview = definePreview(resourcesListArguments, List);
