import type { JSX } from "react";
import { z } from "zod";

import { Chip } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

// A nonempty description cannot be reviewed safely in a few lines. These calls stay expanded.
// Other optional arguments must all be displayed, including review requests and edit permissions.
const createArgs = {
  owner: z.string().min(1),
  repo: z.string().min(1),
  title: z.string().min(1),
  head: z.string().min(1),
  base: z.string().min(1),
  body: z.string().optional(),
  draft: z.boolean().optional(),
  maintainer_can_modify: z.boolean().optional(),
  reviewers: z.array(z.string().min(1)).optional(),
};
const createPullRequest = z.strictObject(createArgs);
const approvablePullRequest = z.strictObject({
  ...createArgs,
  title: z.string().min(1).max(120),
  body: z.literal("").optional(),
  reviewers: z.array(z.string().min(1)).max(4).optional(),
});

function CreatePullRequest({ args }: { args: z.infer<typeof createPullRequest> }): JSX.Element {
  return (
    <>
      <Chip label="repository" value={`${args.owner}/${args.repo}`} />
      <Chip label="title" value={args.title} />
      <Chip label="head → base" value={`${args.head} → ${args.base}`} />
      <Chip
        label="description"
        value={args.body === undefined ? "omitted" : args.body === "" ? "empty" : "open Review"}
      />
      <Chip label="draft" value={args.draft === undefined ? "default" : args.draft ? "yes" : "no"} />
      <Chip
        label="maintainer edits"
        value={args.maintainer_can_modify === undefined ? "default" : args.maintainer_can_modify ? "yes" : "no"}
      />
      <Chip label="reviewers" value={args.reviewers === undefined ? "omitted" : args.reviewers.join(", ") || "none"} />
    </>
  );
}

export const createPullRequestPane: ArgumentsPreview = definePreview(createPullRequest, CreatePullRequest);
export function canApprovePullRequestInline(args: unknown): boolean {
  return approvablePullRequest.safeParse(args).success;
}
