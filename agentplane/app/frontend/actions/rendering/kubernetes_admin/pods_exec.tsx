import { Code, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { argvText } from "../../../argv_text";
import { ShellCommandView, ShellCommandSummary, OutputBlock } from "../../../shell_command_view";

import { definePreview, type ArgumentsPreview } from "../entry";
import { defineResultPreview, type ResultPreview, type ResultPreviewProps } from "../result_entry";
import { podsExecNotes } from "../../schemas/kubernetes_admin/pods_exec";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

const podsExecArguments = z.strictObject({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  container: z.string().min(1).optional(),
  command: z.array(z.string()),
});

type PodsExecArguments = z.infer<typeof podsExecArguments>;

function Collapsed({ args }: { args: PodsExecArguments }): JSX.Element {
  return <ShellCommandSummary text={argvText(args.command)} />;
}

function PodsExecLabel({ args }: { args: PodsExecArguments }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      Run command in <Code>{kubernetesTarget("Pod", args.name, args.namespace)}</Code>
    </Text>
  );
}

function Opened({ args }: { args: PodsExecArguments }): JSX.Element {
  return <ShellCommandView argv={args.command} notes={podsExecNotes(args)} releaseWhenFar surface="framed" />;
}

function PodExecOutput({ result }: ResultPreviewProps<string>): JSX.Element {
  return <OutputBlock name="Output" text={result} raw releaseWhenFar surface="framed" />;
}

const podExecOutput: ResultPreview = defineResultPreview(z.string(), PodExecOutput);

/** These are action-owned DOM renderers; the target is already in the action title. */
export const podsExecCollapsed: ArgumentsPreview = definePreview(podsExecArguments, Collapsed);
export const podsExecPane: ArgumentsPreview = definePreview(podsExecArguments, Opened);
export const podsExecLabel: ArgumentsPreview = definePreview(podsExecArguments, PodsExecLabel);

const podsDeleteArguments = z.strictObject({ name: z.string().min(1), namespace: z.string().min(1).optional() });
const podsDeleteResult = z.literal("Pod deleted successfully");

export function offersCardDecisionButtonsPodsDelete(args: unknown): boolean {
  return podsDeleteArguments.safeParse(args).success;
}

function PodsDeleteLabel({ args }: { args: z.infer<typeof podsDeleteArguments> }): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      Delete <Code>{kubernetesTarget("Pod", args.name, args.namespace)}</Code>
    </Text>
  );
}

export const podsDeleteLabel: ArgumentsPreview = definePreview(podsDeleteArguments, PodsDeleteLabel);
// Direct previews preserve null as the slot suppression signal after schema validation.
export const podsDeletePane: ArgumentsPreview = { schema: podsDeleteArguments, render: () => null };
const podsDeleteResultPreview: ResultPreview = { schema: podsDeleteResult, render: () => null };

export const podsDeletePresentation: ActionPresentation = {
  outcome: (value) =>
    podsDeleteResult.safeParse(value).success ? { label: "Deletion accepted", failed: false } : undefined,
  label: podsDeleteLabel,
  details: {
    arguments: podsDeletePane,
    result: podsDeleteResultPreview,
  },
  offersCardDecisionButtons: offersCardDecisionButtonsPodsDelete,
  compactCardExpands: expandsForRejectedArguments(podsDeleteArguments),
};

export const podsExecPresentation: ActionPresentation = {
  label: podsExecLabel,
  pane: { collapsed: podsExecCollapsed, opened: podsExecPane },
  details: { arguments: podsExecPane, result: podExecOutput },
};
