import type { ActionPresentation } from "../presentation";
import { createBranchPresentation } from "./create_branch";
import { createPullRequestPresentation } from "./create_pull_request";
import { pushFilesPresentation } from "./push_files";
import { updatePullRequestPresentation } from "./update_pull_request";

export const githubRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["push_files", pushFilesPresentation],
  ["update_pull_request", updatePullRequestPresentation],
  ["create_branch", createBranchPresentation],
  ["create_pull_request", createPullRequestPresentation],
]);
