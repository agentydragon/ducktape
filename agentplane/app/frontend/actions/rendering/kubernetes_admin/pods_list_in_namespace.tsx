import { resourceListResult } from "./list_result";
import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import type { z } from "zod";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { zPodsInNamespaceArguments } from "../../schemas/kubernetes_admin/pods_list_in_namespace";

export function offersCardDecisionButtonsPodsInNamespace(args: unknown): boolean {
  return zPodsInNamespaceArguments.safeParse(args).success;
}

function podsInNamespaceTitleIsRedundant(title: string, args: unknown): boolean {
  const parsed = zPodsInNamespaceArguments.safeParse(args);
  if (!parsed.success) return false;
  const normalizedTitle = title.trim().replace(/\s+/g, " ").toLowerCase();
  const actionLabel = `List pods in namespace ${parsed.data.namespace}`.toLowerCase();
  return normalizedTitle === actionLabel;
}

function PodsInNamespaceFilters({ args }: { args: z.infer<typeof zPodsInNamespaceArguments> }): JSX.Element | null {
  const { fieldSelector, labelSelector } = args;
  if (fieldSelector === undefined && labelSelector === undefined) return null;
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      Matching pods where{" "}
      {fieldSelector !== undefined && (
        <>
          field selector <Code>{fieldSelector}</Code>
        </>
      )}
      {fieldSelector !== undefined && labelSelector !== undefined ? " and " : ""}
      {labelSelector !== undefined && (
        <>
          label selector <Code>{labelSelector}</Code>
        </>
      )}
    </Text>
  );
}

export const podsInNamespaceDetails: ArgumentsPreview = definePreview(
  zPodsInNamespaceArguments,
  PodsInNamespaceFilters
);

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

// This is intentionally a separate widget from the details view: each Action owns both
// representations. Card decision buttons eligibility lives in a separate capability registry.
function PodsInNamespaceCollapsed({ args }: { args: z.infer<typeof zPodsInNamespaceArguments> }): JSX.Element {
  return args.fieldSelector !== undefined || args.labelSelector !== undefined ? (
    <PodsInNamespaceFilters args={args} />
  ) : (
    <Text size="sm">List every pod in this namespace.</Text>
  );
}

export const podsInNamespaceCollapsed: ArgumentsPreview = definePreview(
  zPodsInNamespaceArguments,
  PodsInNamespaceCollapsed
);

export const podsInNamespacePresentation: ActionPresentation = {
  label: podsInNamespaceLabel,
  requestTitleIsRedundant: podsInNamespaceTitleIsRedundant,
  pane: { collapsed: podsInNamespaceCollapsed },
  details: { arguments: podsInNamespaceDetails, envelopeResult: resourceListResult },
  offersCardDecisionButtons: offersCardDecisionButtonsPodsInNamespace,
  compactCardExpands: expandsForRejectedArguments(zPodsInNamespaceArguments),
};
