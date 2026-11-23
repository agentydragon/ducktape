import type { JSX } from "react";
import { Code, Text } from "@mantine/core";
import { branchCreated } from "./receipts";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
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

export const createBranchLabel: ArgumentsPreview = definePreview(zCreateBranchArguments, CreateBranchLabel);
// The label already names every validated argument.
const createBranchArguments: ArgumentsPreview = { schema: zCreateBranchArguments, render: () => null };

export const createBranchPresentation: ActionPresentation = {
  fallbackLabel: "Create GitHub branch",
  label: createBranchLabel,
  details: { arguments: createBranchArguments, result: branchCreated },
  offersCardDecisionButtons: offersCardDecisionButtonsCreateBranch,
  compactCardExpands: expandsForRejectedArguments(zCreateBranchArguments),
};
