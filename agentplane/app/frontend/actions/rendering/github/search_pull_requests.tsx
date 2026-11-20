import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
import { pullRequestSearchResults } from "./receipts";
import {
  type SearchPullRequestsArguments,
  zSearchPullRequestsArguments,
} from "../../schemas/github/search_pull_requests";

function SearchPullRequestsLabel(): JSX.Element {
  return (
    <Text size="sm" fw={600}>
      Search GitHub pull requests
    </Text>
  );
}

function SearchPullRequestsArgumentsView({ args }: { args: SearchPullRequestsArguments }): JSX.Element {
  return (
    <Stack gap={2}>
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        Query <Code style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{args.query}</Code>
      </Text>
      {args.owner !== undefined && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {args.repo === undefined ? "Owner" : "Repository"}{" "}
          <Code>
            {args.owner}
            {args.repo === undefined ? "" : `/${args.repo}`}
          </Code>
        </Text>
      )}
      {args.perPage !== undefined && <Text size="sm">Up to {args.perPage} results per page</Text>}
    </Stack>
  );
}

export function offersCardDecisionButtonsSearchPullRequests(args: unknown): boolean {
  return zSearchPullRequestsArguments.safeParse(args).success;
}

export const searchPullRequestsLabel: ArgumentsPreview = definePreview(
  zSearchPullRequestsArguments,
  SearchPullRequestsLabel
);
export const searchPullRequestsCompactArguments: ArgumentsPreview = definePreview(
  zSearchPullRequestsArguments,
  SearchPullRequestsArgumentsView
);
export const searchPullRequestsPresentation: ActionPresentation = {
  fallbackLabel: "Search GitHub pull requests",
  label: searchPullRequestsLabel,
  pane: { collapsed: searchPullRequestsCompactArguments },
  details: { arguments: searchPullRequestsCompactArguments, result: pullRequestSearchResults },
  offersCardDecisionButtons: offersCardDecisionButtonsSearchPullRequests,
  compactCardExpands: expandsForRejectedArguments(zSearchPullRequestsArguments),
};
