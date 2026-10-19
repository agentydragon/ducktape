import type { ActionPresentation } from "../presentation";
import { createPullRequestPresentation } from "./create_pull_request";

export const githubRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["create_pull_request", createPullRequestPresentation],
]);
