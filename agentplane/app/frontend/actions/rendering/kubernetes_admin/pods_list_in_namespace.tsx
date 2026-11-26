import { resourceListResult } from "./list_result";
import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import type { z } from "zod";

import { type ActionPresentation, expandsForDetails } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { podsInNamespaceArguments } from "../../schemas/kubernetes_admin/arguments";

export function offersCardDecisionButtonsPodsInNamespace(args: unknown): boolean {
  return podsInNamespaceArguments.safeParse(args).success;
}

function podsInNamespaceTitleIsRedundant(title: string, args: unknown): boolean {
  const parsed = podsInNamespaceArguments.safeParse(args);
  if (!parsed.success) return false;
  const normalizedTitle = title.trim().replace(/\s+/g, " ").toLowerCase();
  const actionLabel = `List pods in namespace ${parsed.data.namespace}`.toLowerCase();
  return normalizedTitle === actionLabel;
}

function PodsInNamespaceFilters({ args }: { args: z.infer<typeof podsInNamespaceArguments> }): JSX.Element | null {
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

export const podsInNamespaceDetails: ArgumentsPreview = definePreview(podsInNamespaceArguments, PodsInNamespaceFilters);

function PodsInNamespaceLabel({ args }: { args: z.infer<typeof podsInNamespaceArguments> }): JSX.Element {
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

export const podsInNamespaceLabel: ArgumentsPreview = definePreview(podsInNamespaceArguments, PodsInNamespaceLabel);

export const podsInNamespacePresentation: ActionPresentation = {
  label: podsInNamespaceLabel,
  requestTitleIsRedundant: podsInNamespaceTitleIsRedundant,
  pane: { collapsed: null, opened: podsInNamespaceDetails },
  details: { arguments: podsInNamespaceDetails, envelopeResult: resourceListResult },
  offersCardDecisionButtons: offersCardDecisionButtonsPodsInNamespace,
  compactCardExpands: expandsForDetails(
    podsInNamespaceArguments,
    (args) => args.fieldSelector !== undefined || args.labelSelector !== undefined
  ),
};
