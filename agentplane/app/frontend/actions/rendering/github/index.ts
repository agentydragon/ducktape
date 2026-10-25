import type { ActionPresentation } from "../presentation";
import { createBranchPresentation } from "./create_branch";
import { createOrUpdateFilePresentation } from "./create_or_update_file";
import { createPullRequestPresentation } from "./create_pull_request";
import { deleteFilePresentation } from "./delete_file";
import { pushFilesPresentation } from "./push_files";
import { searchPullRequestsPresentation } from "./search_pull_requests";
import { updatePullRequestPresentation } from "./update_pull_request";
import { updatePullRequestBranchPresentation } from "./update_pull_request_branch";

export const githubRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["create_or_update_file", createOrUpdateFilePresentation],
  ["push_files", pushFilesPresentation],
  ["delete_file", deleteFilePresentation],
  ["update_pull_request", updatePullRequestPresentation],
  ["update_pull_request_branch", updatePullRequestBranchPresentation],
  ["create_branch", createBranchPresentation],
  ["create_pull_request", createPullRequestPresentation],
  ["search_pull_requests", searchPullRequestsPresentation],
]);
