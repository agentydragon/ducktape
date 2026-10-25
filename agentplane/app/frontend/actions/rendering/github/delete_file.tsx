import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";

import { definePreview, type ArgumentsPreview } from "../entry";
import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { type DeleteFileArguments, zDeleteFileArguments } from "../../schemas/github/delete_file";

function DeleteFileLabel({ args }: { args: DeleteFileArguments }): JSX.Element {
  return (
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      Delete <Code>{args.path}</Code> in{" "}
      <Code>
        {args.owner}/{args.repo}
      </Code>
    </Text>
  );
}

function DeleteFileArgumentsView({ args }: { args: DeleteFileArguments }): JSX.Element {
  return (
    <Stack gap={2}>
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        From <Code>{args.branch}</Code>
      </Text>
      <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        Commit message: {args.message}
      </Text>
    </Stack>
  );
}

export const deleteFileLabel: ArgumentsPreview = definePreview(zDeleteFileArguments, DeleteFileLabel);
export const deleteFileArguments: ArgumentsPreview = definePreview(zDeleteFileArguments, DeleteFileArgumentsView);

export const deleteFilePresentation: ActionPresentation = {
  fallbackLabel: "Delete a GitHub file",
  label: deleteFileLabel,
  pane: { collapsed: deleteFileArguments },
  details: { arguments: deleteFileArguments },
  offersCardDecisionButtons: (args) => zDeleteFileArguments.safeParse(args).success,
  compactCardExpands: expandsForRejectedArguments(zDeleteFileArguments),
};
