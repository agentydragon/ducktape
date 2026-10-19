import {
  offersCardDecisionButtonsPullRequest,
  createPullRequestLabel,
  createPullRequestPane,
} from "./create_pull_request";
import type { ActionPresentation } from "../presentation";

export const githubRenderers: ReadonlyMap<string, ActionPresentation> = new Map<string, ActionPresentation>([
  [
    "create_pull_request",
    {
      label: createPullRequestLabel,
      pane: { opened: createPullRequestPane },
      details: { arguments: createPullRequestPane },
      offersCardDecisionButtons: offersCardDecisionButtonsPullRequest,
    },
  ],
]);
