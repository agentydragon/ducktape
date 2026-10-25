import { z } from "zod";

const pullRequestFields = [
  "number",
  "title",
  "body",
  "state",
  "state_reason",
  "draft",
  "locked",
  "html_url",
  "user",
  "author_association",
  "labels",
  "assignee",
  "assignees",
  "milestone",
  "comments",
  "reactions",
  "created_at",
  "updated_at",
  "closed_at",
  "closed_by",
  "pull_request",
  "repository_url",
] as const;

export type SearchPullRequestsArguments = {
  query: string;
  owner?: string | undefined;
  repo?: string | undefined;
  page?: number | undefined;
  perPage?: number | undefined;
  sort?:
    | "comments"
    | "reactions"
    | "reactions-+1"
    | "reactions--1"
    | "reactions-smile"
    | "reactions-thinking_face"
    | "reactions-heart"
    | "reactions-tada"
    | "interactions"
    | "created"
    | "updated"
    | undefined;
  order?: "asc" | "desc" | undefined;
  fields?: (typeof pullRequestFields)[number][] | undefined;
};

// Keep in sync with the GitHub MCP search_pull_requests input schema. Unknown arguments fail closed
// rather than hiding a search filter from the compact card.
export const zSearchPullRequestsArguments: z.ZodType<SearchPullRequestsArguments> = z.strictObject({
  query: z.string(),
  owner: z.string().optional(),
  repo: z.string().optional(),
  page: z.number().min(1).optional(),
  perPage: z.number().min(1).max(100).optional(),
  sort: z
    .enum([
      "comments",
      "reactions",
      "reactions-+1",
      "reactions--1",
      "reactions-smile",
      "reactions-thinking_face",
      "reactions-heart",
      "reactions-tada",
      "interactions",
      "created",
      "updated",
    ])
    .optional(),
  order: z.enum(["asc", "desc"]).optional(),
  fields: z.array(z.enum(pullRequestFields)).optional(),
});
