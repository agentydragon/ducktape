import { z } from "zod";

export type UpdatePullRequestArguments = {
  owner: string;
  repo: string;
  pullNumber: number;
  title?: string | undefined;
  body?: string | undefined;
  state?: "open" | "closed" | undefined;
  draft?: boolean | undefined;
  base?: string | undefined;
  maintainer_can_modify?: boolean | undefined;
  reviewers?: string[] | undefined;
};

// Unknown arguments fail closed rather than hiding a parameter the views do not show.
export const zUpdatePullRequestArguments: z.ZodType<UpdatePullRequestArguments> = z.strictObject({
  owner: z.string().min(1),
  repo: z.string().min(1),
  pullNumber: z.int().positive(),
  title: z.string().min(1).optional(),
  body: z.string().optional(),
  state: z.enum(["open", "closed"]).optional(),
  draft: z.boolean().optional(),
  base: z.string().min(1).optional(),
  maintainer_can_modify: z.boolean().optional(),
  reviewers: z.array(z.string().min(1)).optional(),
});

/** A state change leads the label; every other update is listed after it. */
export function pullRequestUpdateVerb(args: UpdatePullRequestArguments): string {
  if (args.state === "closed") return "Close";
  if (args.state === "open") return "Reopen";
  return "Update";
}

/** What the call changes besides the state, as lowercase phrases. Reviewers come last so their
 * comma-separated names cannot read as separate changes. */
export function pullRequestUpdateChanges(args: UpdatePullRequestArguments): string[] {
  return [
    ...(args.title === undefined ? [] : ["retitle"]),
    ...(args.body === undefined ? [] : [args.body === "" ? "clear description" : "replace description"]),
    ...(args.base === undefined ? [] : [`change base to ${args.base}`]),
    ...(args.draft === undefined ? [] : [args.draft ? "mark as draft" : "mark ready for review"]),
    ...(args.maintainer_can_modify === undefined
      ? []
      : [args.maintainer_can_modify ? "allow maintainer edits" : "disallow maintainer edits"]),
    ...(args.reviewers === undefined || args.reviewers.length === 0
      ? []
      : [`request review from ${args.reviewers.join(", ")}`]),
  ];
}
