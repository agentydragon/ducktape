import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import type { z } from "zod";

import { definePreview, type ArgumentsPreview } from "../entry";
import { zPodsInNamespaceArguments } from "../../schemas/kubernetes_admin/pods_list_in_namespace";

export function canQuickApprovePodsInNamespace(args: unknown): boolean {
  return zPodsInNamespaceArguments.safeParse(args).success;
}

function PodsInNamespace({ args }: { args: z.infer<typeof zPodsInNamespaceArguments> }): JSX.Element {
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

export const podsInNamespacePreview: ArgumentsPreview = definePreview(zPodsInNamespaceArguments, PodsInNamespace);

function PodsInNamespaceLabel({ args }: { args: z.infer<typeof zPodsInNamespaceArguments> }): JSX.Element {
  return (
    <Text
      component="span"
      size="sm"
      fw={600}
      style={{ fontFamily: "var(--mantine-font-family)", overflowWrap: "anywhere" }}
    >
      List pods in namespace <Code>{args.namespace}</Code>
    </Text>
  );
}

export const podsInNamespaceLabel: ArgumentsPreview = definePreview(zPodsInNamespaceArguments, PodsInNamespaceLabel);

// This is intentionally a separate widget from the opened pane: each Action owns both
// representations. Quick-approval eligibility lives in a separate capability registry.
function PodsInNamespaceCollapsed({ args }: { args: z.infer<typeof zPodsInNamespaceArguments> }): JSX.Element {
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
  zPodsInNamespaceArguments,
  PodsInNamespaceCollapsed
);
