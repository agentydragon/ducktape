import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import { Chip } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";
import { kubernetesTarget } from "./resource_common";

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

function PodsLogLabel({ args }: { args: z.infer<typeof podsLogArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      View logs for <Code>{kubernetesTarget("Pod", args.name, args.namespace)}</Code>
    </Text>
  );
}

export const podsLogLabel: ArgumentsPreview = definePreview(podsLogArguments, PodsLogLabel);
export const podsLogPreview: ArgumentsPreview = definePreview(podsLogArguments, Logs);
