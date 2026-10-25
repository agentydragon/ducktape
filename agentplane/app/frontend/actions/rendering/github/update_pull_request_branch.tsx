import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import {
  type UpdatePullRequestBranchArguments,
  zUpdatePullRequestBranchArguments,
} from "../../schemas/github/update_pull_request_branch";

function UpdatePullRequestBranchLabel({ args }: { args: UpdatePullRequestBranchArguments }): JSX.Element {
  return (
    <Stack gap={2}>
      <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
        Update branch of{" "}
        <Code>
          {args.owner}/{args.repo}#{args.pullNumber}
        </Code>{" "}
        with latest base changes
      </Text>
      {args.expectedHeadSha !== undefined && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Expected PR HEAD SHA: <Code>{args.expectedHeadSha}</Code>
        </Text>
      )}
    </Stack>
  );
}

function NoFurtherArguments(): JSX.Element {
  return <></>;
}

export const updatePullRequestBranchLabel: ArgumentsPreview = definePreview(
  zUpdatePullRequestBranchArguments,
  UpdatePullRequestBranchLabel
);
export const updatePullRequestBranchArguments: ArgumentsPreview = definePreview(
  zUpdatePullRequestBranchArguments,
  NoFurtherArguments
);

export function offersCardDecisionButtonsUpdatePullRequestBranch(args: unknown): boolean {
  return zUpdatePullRequestBranchArguments.safeParse(args).success;
}

export const updatePullRequestBranchPresentation: ActionPresentation = {
  fallbackLabel: "Update GitHub pull request branch",
  label: updatePullRequestBranchLabel,
  details: { arguments: updatePullRequestBranchArguments },
  offersCardDecisionButtons: offersCardDecisionButtonsUpdatePullRequestBranch,
  compactCardExpands: expandsForRejectedArguments(zUpdatePullRequestBranchArguments),
};
