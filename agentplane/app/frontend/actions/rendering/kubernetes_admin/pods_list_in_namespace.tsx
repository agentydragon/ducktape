import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { definePreview, type ArgumentsPreview } from "../entry";

// Unknown arguments fail closed rather than making an unshown parameter actionable.
const podsInNamespaceArguments = z.strictObject({
  namespace: z.string().min(1),
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
});

export function canQuickApprovePodsInNamespace(args: unknown): boolean {
  return podsInNamespaceArguments.safeParse(args).success;
}

function PodsInNamespace({ args }: { args: z.infer<typeof podsInNamespaceArguments> }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      List pods in namespace <Code>{args.namespace}</Code>
      {args.fieldSelector !== undefined && (
        <>
          {" · field selector "}
          <Code>{args.fieldSelector}</Code>
        </>
      )}
      {args.labelSelector !== undefined && (
        <>
          {" · label selector "}
          <Code>{args.labelSelector}</Code>
        </>
      )}
    </Text>
  );
}

export const podsInNamespacePreview: ArgumentsPreview = definePreview(podsInNamespaceArguments, PodsInNamespace);

// This is intentionally a separate widget from the opened pane: each Action owns both
// representations. Quick-approval eligibility lives in a separate capability registry.
function PodsInNamespaceCollapsed({ args }: { args: z.infer<typeof podsInNamespaceArguments> }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Get pods · namespace <Code>{args.namespace}</Code>
      {args.fieldSelector !== undefined && (
        <>
          {" · field selector "}
          <Code>{args.fieldSelector}</Code>
        </>
      )}
      {args.labelSelector !== undefined && (
        <>
          {" · label selector "}
          <Code>{args.labelSelector}</Code>
        </>
      )}
    </Text>
  );
}

export const podsInNamespaceCollapsed: ArgumentsPreview = definePreview(
  podsInNamespaceArguments,
  PodsInNamespaceCollapsed
);
