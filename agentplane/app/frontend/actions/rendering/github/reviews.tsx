import { Code, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { TextBlock } from "../../../ui/text_block";
import type { ActionOutcome, ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreviewProps } from "../result_entry";
import {
  account,
  acknowledgement,
  byMethod,
  displayState,
  Field,
  githubPresentation,
  GitHubItem,
  pageInput,
  pagination,
  RepoTarget,
  repoInput,
  ResultRows,
} from "./display";
import { GitHubLink, pullRequestLink, repositoryHref } from "./receipts";

const target = repoInput.extend({ pullNumber: z.int().positive() });
const pullRequests = z
  .object({
    number: z.int().positive().optional(),
    title: z.string().optional(),
    body: z.string().optional(),
    html_url: z.string().optional(),
    state: z.string().optional(),
    draft: z.boolean().optional(),
    merged: z.boolean().optional(),
    user: account.nullish(),
    base: z.object({ ref: z.string() }).nullish(),
    head: z.object({ ref: z.string() }).nullish(),
    requested_reviewers: z.array(z.string()).optional(),
  })
  .refine((pr) => pr.number !== undefined || Boolean(pr.title || pr.html_url));
type PullRequest = z.infer<typeof pullRequests>;

function PullTarget({ args }: { args: z.infer<typeof target> }): JSX.Element {
  return (
    <Text size="sm">
      <GitHubLink href={pullRequestLink(args)}>
        {args.owner}/{args.repo}#{args.pullNumber}
      </GitHubLink>
    </Text>
  );
}

function PullRequestCard({ pr, detail = false }: { pr: PullRequest; detail?: boolean }): JSX.Element {
  const branch = pr.base && pr.head ? `${pr.head.ref} → ${pr.base.ref}` : undefined;
  return (
    <GitHubItem
      title={pr.title || `Pull request${pr.number ? ` #${pr.number}` : ""}`}
      href={pr.html_url}
      info={[pr.merged ? "Merged" : pr.draft ? "Draft" : displayState(pr.state), pr.user?.login, branch]}
    >
      {detail && <Field name="Reviewers" value={pr.requested_reviewers?.join(", ")} />}
      {detail && pr.body && <TextBlock text={pr.body} heading="Description" format="prose" />}
    </GitHubItem>
  );
}

const listPulls = repoInput.extend({
  ...pageInput,
  fields: z.array(z.string()).optional(),
  base: z.string().optional(),
  head: z.string().optional(),
  state: z.enum(["open", "closed", "all"]).optional(),
  direction: z.enum(["asc", "desc"]).optional(),
  sort: z.enum(["created", "updated", "popularity", "long-running"]).optional(),
});
const listPullsResult = defineResultPreview(z.array(pullRequests).nullable(), ({ result }) => (
  <ResultRows count={result?.length ?? 0} name="pull requests">
    {result?.map((pr, index) => (
      <PullRequestCard key={pr.html_url ?? pr.number ?? index} pr={pr} />
    ))}
  </ResultRows>
));

const readMethods = {
  get: "Details and description",
  get_diff: "Full patch",
  get_status: "Commit statuses",
  get_files: "Changed files",
  get_commits: "Commits",
  get_review_comments: "Review threads",
  get_reviews: "Submitted reviews",
  get_comments: "Conversation comments",
  get_check_runs: "Checks",
} as const;
const readPull = target.extend({
  ...pageInput,
  after: z.string().optional(),
  method: z.enum(Object.keys(readMethods) as [keyof typeof readMethods, ...(keyof typeof readMethods)[]]),
});
const diff = z.union([z.string(), z.object({ diff: z.string() })]);
const pullFiles = z.array(
  z.object({
    filename: z.string(),
    status: z.string().optional(),
    additions: z.int().optional(),
    deletions: z.int().optional(),
    patch: z.string().optional(),
    previous_filename: z.string().optional(),
  })
);
const pullCommits = z.array(
  z.object({
    sha: z.string(),
    message: z.string().optional(),
    html_url: z.string().optional(),
    author: z.object({ name: z.string().optional() }).nullish(),
  })
);
const comments = z.array(
  z.object({
    id: z.int(),
    body: z.string().optional(),
    state: z.string().optional(),
    user: account.nullish(),
    html_url: z.string().optional(),
  })
);
const status = z.object({
  state: z.string(),
  total_count: z.int().optional(),
  statuses: z.array(
    z.object({
      state: z.string(),
      context: z.string(),
      description: z.string().optional(),
      target_url: z.string().optional(),
    })
  ),
});
const checks = z.object({
  total_count: z.int().optional(),
  check_runs: z.array(
    z.object({
      name: z.string(),
      status: z.string().optional(),
      conclusion: z.string().optional(),
      html_url: z.string().optional(),
    })
  ),
});
const reviewThreads = z.object({
  totalCount: z.int().optional(),
  pageInfo: pagination.optional(),
  review_threads: z.array(
    z.object({
      id: z.string(),
      is_resolved: z.boolean(),
      is_outdated: z.boolean(),
      total_count: z.int().optional(),
      comments: z.array(
        z.object({
          path: z.string(),
          line: z.int().nullish(),
          body: z.string().optional(),
          author: z.string().optional(),
          html_url: z.string().optional(),
        })
      ),
    })
  ),
});
const reviewCommentRows = (name: string) =>
  defineResultPreview(comments.nullable(), ({ result }) => (
    <ResultRows count={result?.length ?? 0} name={name}>
      {result?.map((comment) => (
        <GitHubItem
          key={comment.id}
          title={comment.user?.login || "GitHub user"}
          href={comment.html_url}
          info={[displayState(comment.state)]}
        >
          {comment.body && <TextBlock text={comment.body} format="prose" />}
        </GitHubItem>
      ))}
    </ResultRows>
  ));

const pullReadResult = byMethod({
  get: defineResultPreview(pullRequests, ({ result }) => <PullRequestCard pr={result} detail />),
  get_diff: defineResultPreview(diff, ({ result }) => (
    <TextBlock text={typeof result === "string" ? result : result.diff} heading="Patch" />
  )),
  get_status: defineResultPreview(status, ({ result }) => (
    <Stack gap="xs">
      <Field name="Combined status" value={displayState(result.state)} />
      <ResultRows count={result.statuses.length} total={result.total_count} name="statuses">
        {result.statuses.map((status, i) => (
          <GitHubItem
            key={i}
            title={status.context}
            href={status.target_url}
            info={[displayState(status.state), status.description]}
          />
        ))}
      </ResultRows>
    </Stack>
  )),
  get_files: defineResultPreview(pullFiles.nullable(), ({ result }) => (
    <ResultRows count={result?.length ?? 0} name="changed files">
      {result?.map((file) => (
        <GitHubItem
          key={file.filename}
          title={file.filename}
          info={[
            displayState(file.status),
            file.previous_filename ? `From ${file.previous_filename}` : undefined,
            file.additions !== undefined ? `+${file.additions}` : undefined,
            file.deletions !== undefined ? `−${file.deletions}` : undefined,
          ]}
        >
          {file.patch && <TextBlock text={file.patch} heading="Patch" />}
        </GitHubItem>
      ))}
    </ResultRows>
  )),
  get_commits: defineResultPreview(pullCommits.nullable(), ({ result }) => (
    <ResultRows count={result?.length ?? 0} name="commits">
      {result?.map((commit) => (
        <GitHubItem
          key={commit.sha}
          title={commit.message?.split("\n")[0] || commit.sha.slice(0, 7)}
          href={commit.html_url}
          info={[commit.author?.name]}
        />
      ))}
    </ResultRows>
  )),
  get_review_comments: defineResultPreview(reviewThreads, ({ result }) => (
    <ResultRows
      count={result.review_threads.length}
      total={result.totalCount}
      more={result.pageInfo?.hasNextPage}
      name="review threads"
    >
      {result.review_threads.map((thread) => (
        <Stack gap={4} key={thread.id}>
          <Text size="xs" c="dimmed">
            {thread.is_resolved ? "Resolved" : "Unresolved"}
            {thread.is_outdated ? " · Outdated" : ""}
          </Text>
          {thread.comments.map((comment, i) => (
            <GitHubItem
              key={i}
              title={comment.line ? `${comment.path}:${comment.line}` : comment.path}
              href={comment.html_url}
              info={[comment.author]}
            >
              {comment.body && <TextBlock text={comment.body} format="prose" />}
            </GitHubItem>
          ))}
        </Stack>
      ))}
    </ResultRows>
  )),
  get_reviews: reviewCommentRows("reviews"),
  get_comments: reviewCommentRows("comments"),
  get_check_runs: defineResultPreview(checks, ({ result }) => (
    <ResultRows count={result.check_runs.length} total={result.total_count} name="checks">
      {result.check_runs.map((check, i) => (
        <GitHubItem
          key={i}
          title={check.name}
          href={check.html_url}
          info={[displayState(check.conclusion || check.status)]}
        />
      ))}
    </ResultRows>
  )),
});

const merge = target.extend({
  commit_title: z.string().optional(),
  commit_message: z.string().optional(),
  expectedHeadSha: z.string().optional(),
  merge_method: z.enum(["merge", "squash", "rebase"]).optional(),
});
const mergeReply = z.object({
  merged: z.boolean(),
  message: z.string().optional(),
  sha: z
    .string()
    .regex(/^[a-f0-9]+$/i)
    .optional(),
});
function MergeReceipt({ result, context }: ResultPreviewProps<z.infer<typeof mergeReply>>): JSX.Element {
  const args = target.safeParse(context?.arguments);
  return (
    <Stack gap={4}>
      <Text size="sm">{result.merged ? "Pull request merged." : "GitHub did not merge the pull request."}</Text>
      {result.message && <Text size="sm">{result.message}</Text>}
      {result.merged && result.sha && args.success && (
        <Text size="sm">
          <GitHubLink href={`${repositoryHref(args.data)}/commit/${result.sha}`}>Open merge commit</GitHubLink>
        </Text>
      )}
    </Stack>
  );
}

function mergeOutcome(value: unknown): ActionOutcome | undefined {
  const parsed = mergeReply.safeParse(value);
  return parsed.success
    ? { label: parsed.data.merged ? "Merged" : "Not merged", failed: !parsed.data.merged }
    : undefined;
}

const reviewMethod = {
  create: "Create review",
  submit_pending: "Submit pending review",
  delete_pending: "Delete pending review",
  resolve_thread: "Resolve review thread",
  unresolve_thread: "Reopen review thread",
} as const;
const review = target.extend({
  method: z.enum(Object.keys(reviewMethod) as [keyof typeof reviewMethod, ...(keyof typeof reviewMethod)[]]),
  body: z.string().optional(),
  commitID: z.string().optional(),
  event: z.enum(["APPROVE", "REQUEST_CHANGES", "COMMENT"]).optional(),
  threadId: z.string().optional(),
});
const copilotReply = z.union([z.literal(""), z.object({ status: z.literal("requested") })]);
const reviewReply = z.union([z.string(), z.object({ message: z.string() })]);
const reviewComment = target.extend({
  body: z.string(),
  path: z.string(),
  subjectType: z.enum(["FILE", "LINE"]),
  line: z.int().positive().optional(),
  side: z.enum(["LEFT", "RIGHT"]).optional(),
  startLine: z.int().positive().optional(),
  startSide: z.enum(["LEFT", "RIGHT"]).optional(),
});
const reaction = z.enum(["+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"]);
const commentReply = repoInput.extend({
  pullNumber: z.int().positive().optional(),
  commentId: z.int().positive(),
  body: z.string().optional(),
  reaction: reaction.optional(),
});
const postedComment = z.object({
  id: z.union([z.int(), z.string()]),
  html_url: z.string().optional(),
  url: z.string().optional(),
});
const replyResult = z.union([postedComment, z.object({ comment: postedComment, reaction: postedComment })]);

export const reviewRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "list_pull_requests",
    githubPresentation({
      label: "List GitHub pull requests",
      schema: listPulls,
      cardDecision: true,
      result: listPullsResult,
      compact: ({ args }) => <Field name="Repository" value={`${args.owner}/${args.repo}`} />,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/pulls" />
          <Field name="State" value={displayState(args.state ?? "open")} />
          <Field name="Head branch" value={args.head} />
          <Field name="Base branch" value={args.base} />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
    }),
  ],
  [
    "pull_request_read",
    githubPresentation({
      label: "Read GitHub pull request",
      schema: readPull,
      result: pullReadResult,
      cardDecision: true,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo}#{args.pullNumber} · {readMethods[args.method]}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <PullTarget args={args} />
          <Field name="Read" value={readMethods[args.method]} />
          <Field name="Page size" value={args.perPage} />
          {args.after && <Text size="sm">Continues a previous page.</Text>}
        </Stack>
      ),
    }),
  ],
  [
    "merge_pull_request",
    githubPresentation({
      label: "Merge GitHub pull request",
      schema: merge,
      result: defineResultPreview(mergeReply, MergeReceipt),
      outcome: mergeOutcome,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo}#{args.pullNumber}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <PullTarget args={args} />
          <Field
            name="Merge method"
            value={args.merge_method ? displayState(args.merge_method) : "Repository default"}
          />
          <Field name="Only if the PR head is" value={args.expectedHeadSha && <Code>{args.expectedHeadSha}</Code>} />
          <Field name="Commit title" value={args.commit_title} />
          {args.commit_message !== undefined && (
            <TextBlock text={args.commit_message} heading="Commit message" format="prose" />
          )}
        </Stack>
      ),
    }),
  ],
  [
    "request_copilot_review",
    githubPresentation({
      label: "Request Copilot review",
      schema: target,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo}#{args.pullNumber}
        </Text>
      ),
      request: ({ args }) => <PullTarget args={args} />,
      result: defineResultPreview(copilotReply, () => <Text size="sm">Copilot review requested.</Text>),
    }),
  ],
  [
    "pull_request_review_write",
    githubPresentation({
      label: "Change GitHub pull request review",
      schema: review,
      compact: ({ args }) => <Text size="sm">{reviewMethod[args.method]}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          <PullTarget args={args} />
          <Field name="Operation" value={reviewMethod[args.method]} />
          <Field name="Decision" value={displayState(args.event)} />
          <Field name="Commit" value={args.commitID} />
          <Field name="Review thread" value={args.threadId} />
          {args.body !== undefined && <TextBlock text={args.body} heading="Review text" format="prose" />}
        </Stack>
      ),
      result: defineResultPreview(reviewReply, ({ result }) => (
        <Text size="sm">{typeof result === "string" ? result : result.message}</Text>
      )),
    }),
  ],
  [
    "add_comment_to_pending_review",
    githubPresentation({
      label: "Comment on pending GitHub review",
      schema: reviewComment,
      compact: ({ args }) => (
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {args.path}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <PullTarget args={args} />
          <Field
            name={args.subjectType === "FILE" ? "File" : "File and lines"}
            value={args.line ? `${args.path}:${args.startLine ? `${args.startLine}–` : ""}${args.line}` : args.path}
          />
          <Field
            name="Diff side"
            value={args.side ? [args.startSide, args.side].filter(Boolean).join(" → ") : undefined}
          />
          <TextBlock text={args.body} heading="Comment" format="prose" />
        </Stack>
      ),
      result: acknowledgement("pull request review comment successfully added to pending review"),
    }),
  ],
  [
    "add_reply_to_pull_request_comment",
    githubPresentation({
      label: "Reply or react to GitHub review comment",
      schema: commentReply,
      compact: ({ args }) => (
        <Text size="sm">
          {args.reaction && args.body ? "Reply and react" : args.reaction ? "React to comment" : "Reply to comment"}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          {args.pullNumber ? (
            <PullTarget args={{ ...args, pullNumber: args.pullNumber }} />
          ) : (
            <RepoTarget repo={args} />
          )}
          <Field name="Review comment" value={args.commentId} />
          <Field name="Reaction" value={args.reaction} />
          {args.body !== undefined && <TextBlock text={args.body} heading="Reply" format="prose" />}
        </Stack>
      ),
      result: defineResultPreview(replyResult, ({ result, context }) => {
        const args = commentReply.safeParse(context?.arguments);
        if ("comment" in result)
          return <GitHubItem title="Reply and reaction posted" href={result.comment.html_url ?? result.comment.url} />;
        return (
          <GitHubItem
            title={args.success && args.data.body === undefined ? "Reaction added" : "Reply posted"}
            href={result.html_url ?? result.url}
          />
        );
      }),
    }),
  ],
]);
