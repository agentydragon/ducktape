import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import { Chip } from "../chips";
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
    <>
      <Chip label="API version" value={args.apiVersion} />
      <Selectors args={args} />
    </>
  );
}

function ListLabel({ args }: { args: z.infer<typeof resourcesListArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      List <Code>{args.kind}</Code> resources
      {args.namespace && (
        <>
          {" "}
          in namespace <Code>{args.namespace}</Code>
        </>
      )}
    </Text>
  );
}

export const resourcesListLabel: ArgumentsPreview = definePreview(resourcesListArguments, ListLabel);
export const resourcesListPane: ArgumentsPreview = definePreview(resourcesListArguments, List);
