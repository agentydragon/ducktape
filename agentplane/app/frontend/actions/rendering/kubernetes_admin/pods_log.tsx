import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { z } from "zod";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { podsLogOptions } from "../../schemas/kubernetes_admin/pods_log";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

const podsLogArguments = z.strictObject({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  container: z.string().min(1).optional(),
  previous: z.boolean().optional(),
  tail: z.int().optional(),
});

export function offersCardDecisionButtonsPodsLog(args: unknown): boolean {
  return podsLogArguments.safeParse(args).success;
}

function PodsLogLabel({ args }: { args: z.infer<typeof podsLogArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      View logs for <Code>{kubernetesTarget("Pod", args.name, args.namespace)}</Code>
      {podsLogOptions(args)
        .map((option) => `, ${option}`)
        .join("")}
    </Text>
  );
}

export const podsLogLabel: ArgumentsPreview = definePreview(podsLogArguments, PodsLogLabel);
// The label carries every argument; direct previews preserve null as the slot suppression signal.
export const podsLogPreview: ArgumentsPreview = { schema: podsLogArguments, render: () => null };

export const podsLogPresentation: ActionPresentation = {
  label: podsLogLabel,
  details: { arguments: podsLogPreview },
  offersCardDecisionButtons: offersCardDecisionButtonsPodsLog,
  compactCardExpands: expandsForRejectedArguments(podsLogArguments),
};
