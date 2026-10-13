import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import { definePreview, type ArgumentsPreview } from "../entry";
import { kubernetesTarget } from "./resource_common";

const podsGetArguments = z.strictObject({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
});

export function offersCardDecisionButtonsPodsGet(args: unknown): boolean {
  return podsGetArguments.safeParse(args).success;
}

// The label already carries the Pod and namespace; the name and namespace are the only arguments.
function Target(): JSX.Element {
  return (
    <Text size="sm" c="dimmed">
      No additional arguments
    </Text>
  );
}

function PodsGetLabel({ args }: { args: z.infer<typeof podsGetArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      Get <Code>{kubernetesTarget("Pod", args.name, args.namespace)}</Code>
    </Text>
  );
}

export const podsGetLabel: ArgumentsPreview = definePreview(podsGetArguments, PodsGetLabel);
export const podsGetPreview: ArgumentsPreview = definePreview(podsGetArguments, Target);
