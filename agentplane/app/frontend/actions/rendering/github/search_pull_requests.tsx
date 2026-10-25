import type { JSX } from "react";
import { Code, Stack, Text } from "@mantine/core";

import { type ActionPresentation, expandsForRejectedArguments } from "../presentation";
import { definePreview, type ArgumentsPreview } from "../entry";
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

function SearchPullRequestsArgumentsView({
  args,
  showFullOptions,
}: {
  args: SearchPullRequestsArguments;
  showFullOptions: boolean;
}): JSX.Element {
  return (
    <Stack gap={2}>
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        Query: <Code style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{args.query}</Code>
      </Text>
      {args.owner !== undefined && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Owner filter: <Code>{args.owner}</Code>
        </Text>
      )}
      {args.repo !== undefined && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Repository filter: <Code>{args.repo}</Code>
        </Text>
      )}
      {showFullOptions && args.page !== undefined && <Text size="sm">Page {args.page}</Text>}
      {showFullOptions && args.perPage !== undefined && <Text size="sm">{args.perPage} results per page</Text>}
      {showFullOptions && args.sort !== undefined && <Text size="sm">Sort by {args.sort}</Text>}
      {showFullOptions && args.order !== undefined && <Text size="sm">Order: {args.order}</Text>}
      {showFullOptions && args.fields !== undefined && (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          Fields: {args.fields.length > 0 ? args.fields.join(", ") : "none"}
        </Text>
      )}
    </Stack>
  );
}

function SearchPullRequestsCompactArgumentsView({ args }: { args: SearchPullRequestsArguments }): JSX.Element {
  return <SearchPullRequestsArgumentsView args={args} showFullOptions={false} />;
}

function SearchPullRequestsDetailsArgumentsView({ args }: { args: SearchPullRequestsArguments }): JSX.Element {
  return <SearchPullRequestsArgumentsView args={args} showFullOptions={true} />;
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
  SearchPullRequestsCompactArgumentsView
);
export const searchPullRequestsDetailsArguments: ArgumentsPreview = definePreview(
  zSearchPullRequestsArguments,
  SearchPullRequestsDetailsArgumentsView
);

export const searchPullRequestsPresentation: ActionPresentation = {
  label: searchPullRequestsLabel,
  pane: { collapsed: searchPullRequestsCompactArguments, opened: searchPullRequestsCompactArguments },
  details: { arguments: searchPullRequestsDetailsArguments },
  offersCardDecisionButtons: offersCardDecisionButtonsSearchPullRequests,
  compactCardExpands: expandsForRejectedArguments(zSearchPullRequestsArguments),
};
