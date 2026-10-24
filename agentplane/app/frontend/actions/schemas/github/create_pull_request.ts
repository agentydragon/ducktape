import { z } from "zod";

export type CreatePullRequestArguments = {
  owner: string;
  repo: string;
  title: string;
  head: string;
  base: string;
  body?: string | undefined;
  draft?: boolean | undefined;
  maintainer_can_modify?: boolean | undefined;
  reviewers?: string[] | undefined;
};

// Unknown arguments fail closed rather than hiding a parameter the views do not show.
export const zCreatePullRequestArguments: z.ZodType<CreatePullRequestArguments> = z.strictObject({
  owner: z.string().min(1),
  repo: z.string().min(1),
  title: z.string().min(1),
  head: z.string().min(1),
  base: z.string().min(1),
  body: z.string().optional(),
  draft: z.boolean().optional(),
  maintainer_can_modify: z.boolean().optional(),
  reviewers: z.array(z.string().min(1)).optional(),
});

/** The settings shown as chips beside the head branch. A non-draft PR is GitHub's default, so
 * `draft: false` adds none. */
export function pullRequestOptions(args: CreatePullRequestArguments): string[] {
  return [
    ...(args.draft === true ? ["draft"] : []),
    ...(args.maintainer_can_modify === undefined
      ? []
      : [args.maintainer_can_modify ? "maintainers can edit" : "maintainers cannot edit"]),
    ...(args.reviewers === undefined || args.reviewers.length === 0
      ? []
      : [`review from ${args.reviewers.join(", ")}`]),
  ];
}
