import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { ActionJson } from "../../payload";
import { kubernetesTarget } from "./resource_common";

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

function PodsLogCollapsed({ args }: { args: z.infer<typeof podsLogArguments> }): JSX.Element {
  return (
    <Stack gap="xs">
      {args.container && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Container <Code>{args.container}</Code>
        </Text>
      )}
      {args.previous === true && <Text size="sm">Previous logs</Text>}
    </Stack>
  );
}

function PodsLogOpened({ args }: { args: z.infer<typeof podsLogArguments> }): JSX.Element {
  const hasOptions = args.container !== undefined || args.previous !== undefined || args.tail !== undefined;
  return hasOptions ? (
    <ActionJson value={{ container: args.container, previous: args.previous, tail: args.tail }} clamp={false} />
  ) : (
    <></>
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
export const podsLogCollapsed: ArgumentsPreview = definePreview(podsLogArguments, PodsLogCollapsed);
export const podsLogPreview: ArgumentsPreview = definePreview(podsLogArguments, PodsLogOpened);

export const podsLogPresentation: ActionPresentation = {
  label: podsLogLabel,
  pane: { collapsed: podsLogCollapsed, opened: podsLogPreview },
  details: { arguments: podsLogPreview },
  offersCardDecisionButtons: offersCardDecisionButtonsPodsLog,
};
