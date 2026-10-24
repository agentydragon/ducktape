import type { JSX } from "react";
import { Code, Text } from "@mantine/core";

import type { ActionPresentation } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { type CreateBranchArguments, zCreateBranchArguments } from "../../schemas/github/create_branch";

// The label shows every argument, so the compact card is enough to decide on.
export function offersCardDecisionButtonsCreateBranch(args: unknown): boolean {
  return zCreateBranchArguments.safeParse(args).success;
}

function CreateBranchLabel({ args }: { args: CreateBranchArguments }): JSX.Element {
  return (
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      Create branch <Code>{args.branch}</Code> from{" "}
      {args.from_branch === undefined ? "the default branch" : <Code>{args.from_branch}</Code>} in{" "}
      <Code>
        {args.owner}/{args.repo}
      </Code>
    </Text>
  );
}

// Nothing is left once the label has drawn every argument; the schema still gates the empty view.
function NoFurtherArguments(): JSX.Element {
  return <></>;
}

export const createBranchLabel: ArgumentsPreview = definePreview(zCreateBranchArguments, CreateBranchLabel);
export const createBranchArguments: ArgumentsPreview = definePreview(zCreateBranchArguments, NoFurtherArguments);

export const createBranchPresentation: ActionPresentation = {
  fallbackLabel: "Create GitHub branch",
  label: createBranchLabel,
  pane: { opened: createBranchArguments },
  details: { arguments: createBranchArguments },
  offersCardDecisionButtons: offersCardDecisionButtonsCreateBranch,
};
