import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import type { z } from "zod";

import { definePreview, type ArgumentsPreview } from "../entry";
import { zPodsInNamespaceArguments } from "../../schemas/kubernetes_admin/pods_list_in_namespace";

export function canQuickApprovePodsInNamespace(args: unknown): boolean {
  return zPodsInNamespaceArguments.safeParse(args).success;
}

export function podsInNamespaceTitleIsRedundant(title: string, args: unknown): boolean {
  const parsed = zPodsInNamespaceArguments.safeParse(args);
  if (!parsed.success) return false;
  const normalizedTitle = title.trim().replace(/\s+/g, " ").toLowerCase();
  const actionLabel = `List pods in namespace ${parsed.data.namespace}`.toLowerCase();
  return normalizedTitle === actionLabel;
}

function PodsInNamespace({ args }: { args: z.infer<typeof zPodsInNamespaceArguments> }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      List pods in namespace <Code>{args.namespace}</Code>
      {podsInNamespaceFilters(args)}
    </Text>
  );
}

export const podsInNamespacePreview: ArgumentsPreview = definePreview(zPodsInNamespaceArguments, PodsInNamespace);

function podsInNamespaceFilters(args: z.infer<typeof zPodsInNamespaceArguments>): JSX.Element {
  return (
    <>
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
    </>
  );
}

// The pane heading already carries the action label and namespace. Show only the extra criteria
// here, so expanding the card adds information instead of restating its heading.
function PodsInNamespacePane({ args }: { args: z.infer<typeof zPodsInNamespaceArguments> }): JSX.Element {
  const hasFilters = args.fieldSelector !== undefined || args.labelSelector !== undefined;
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      {hasFilters ? podsInNamespaceFilters(args) : "No additional filters"}
    </Text>
  );
}

export const podsInNamespacePane: ArgumentsPreview = definePreview(zPodsInNamespaceArguments, PodsInNamespacePane);

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
  const hasFilters = args.fieldSelector !== undefined || args.labelSelector !== undefined;
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      {hasFilters ? <>Filters{podsInNamespaceFilters(args)}</> : "No additional filters"}
    </Text>
  );
}

export const podsInNamespaceCollapsed: ArgumentsPreview = definePreview(
  zPodsInNamespaceArguments,
  PodsInNamespaceCollapsed
);
