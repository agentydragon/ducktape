import { Badge, isLightColor, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";
import { useQuery } from "@tanstack/react-query";

import { getGitHubLabelSummaries } from "../../client";

import { TextBlock } from "../../../ui/text_block";
import type { ActionPresentation } from "../presentation";
import { definePreview } from "../entry";
import { defineResultPreview, type ResultPreviewProps } from "../result_entry";
import {
  account,
  byMethod,
  displayState,
  Field,
  githubPresentation,
  GitHubItem,
  pageInput,
  pagination,
  RepoSlug,
  RepoTarget,
  repoInput,
  ResultRows,
  type RepoArgs,
} from "./display";
import { awaitingGitHubSubmission, GitHubLink, githubHref, repositoryHref } from "./receipts";

const number = z.int().positive();
const target = repoInput.extend({ issue_number: number });
const namedLabel = z.object({ name: z.string(), description: z.string().nullish(), color: z.string().optional() });
const labels = z.array(z.union([z.string(), namedLabel]));
const issue = z
  .object({
    number: number.optional(),
    title: z.string().optional(),
    body: z.string().nullish(),
    state: z.string().optional(),
    state_reason: z.string().nullish(),
    html_url: z.string().optional(),
    url: z.string().optional(),
    repository: z.string().optional(),
    assignees: z.array(z.union([z.string(), account])).nullish(),
    labels: labels.nullish(),
    issue_type: z.string().nullish(),
    user: account.nullish(),
  })
  .refine((issue) => issue.number !== undefined || Boolean(issue.title || issue.html_url || issue.url));
type Issue = z.infer<typeof issue>;
const issueComment = z.object({
  id: z.union([z.int(), z.string()]),
  html_url: z.string().optional(),
  url: z.string().optional(),
});

function issueHref(args: RepoArgs, number: number): string {
  return `${repositoryHref(args)}/issues/${number}`;
}

function IssueTarget({ args }: { args: z.infer<typeof target> }): JSX.Element {
  return (
    <Text size="sm">
      <GitHubLink href={issueHref(args, args.issue_number)}>
        {args.owner}/{args.repo}#{args.issue_number}
      </GitHubLink>
    </Text>
  );
}

function IssueCard({ item, detail = false }: { item: Issue; detail?: boolean }): JSX.Element {
  const href = githubHref(item.html_url) ?? githubHref(item.url);
  const repo = item.repository ?? (href ? new URL(href).pathname.split("/").slice(1, 3).join("/") : undefined);
  return (
    <GitHubItem
      title={item.title || (item.number === undefined ? "Issue" : `Issue #${item.number}`)}
      href={href}
      info={[repo, displayState(item.state), displayState(item.state_reason), item.issue_type]}
    >
      <Field
        name="Labels"
        value={item.labels?.map((label) => (typeof label === "string" ? label : label.name)).join(", ")}
      />
      <Field
        name="Assignees"
        value={item.assignees
          ?.map((item) => (typeof item === "string" ? item : item.login))
          .filter(Boolean)
          .join(", ")}
      />
      {detail && item.body && <TextBlock text={item.body} heading="Description" format="prose" />}
    </GitHubItem>
  );
}

const readMethods = {
  get: "Issue and description",
  get_comments: "Comments",
  get_sub_issues: "Sub-issues",
  get_parent: "Parent issue",
  get_labels: "Labels",
} as const;
const read = target.extend({
  ...pageInput,
  method: z.enum(Object.keys(readMethods) as [keyof typeof readMethods, ...(keyof typeof readMethods)[]]),
});
const commentList = z.array(
  issueComment.extend({ user: account.nullish(), body: z.string().optional(), created_at: z.string().optional() })
);
const comments = z.union([commentList, z.object({ comments: commentList })]);
const subIssues = z.union([
  z.array(issue.nullable()).nullable(),
  z.object({ sub_issues: z.array(issue.nullable()).nullable() }),
]);
const directIssue = z.union([issue, z.object({ issue: issue.nullable() })]);

const issueReadResult = byMethod({
  get: defineResultPreview(directIssue, ({ result }) => {
    const item = "issue" in result ? result.issue : result;
    return item ? <IssueCard item={item} detail /> : <Text size="sm">No issue returned.</Text>;
  }),
  get_comments: defineResultPreview(comments, ({ result }) => {
    const items = Array.isArray(result) ? result : result.comments;
    return (
      <ResultRows count={items.length} name="comments">
        {items.map((comment) => (
          <GitHubItem
            key={comment.id}
            title={comment.user?.login || "GitHub user"}
            href={comment.html_url ?? comment.url}
            info={[comment.created_at]}
          >
            {comment.body && <TextBlock text={comment.body} format="prose" />}
          </GitHubItem>
        ))}
      </ResultRows>
    );
  }),
  get_sub_issues: defineResultPreview(subIssues, ({ result }) => {
    const items =
      (result === null || Array.isArray(result) ? result : result.sub_issues)?.filter((item) => item !== null) ?? [];
    return (
      <ResultRows count={items.length} name="sub-issues">
        {items.map((item, index) => (
          <IssueCard key={item.number ?? index} item={item} />
        ))}
      </ResultRows>
    );
  }),
  get_parent: defineResultPreview(z.object({ parent: issue.nullable() }), ({ result }) =>
    result.parent ? <IssueCard item={result.parent} /> : <Text size="sm">This issue has no parent.</Text>
  ),
  get_labels: defineResultPreview(z.object({ labels, totalCount: z.int().optional() }), ({ result }) => (
    <ResultRows count={result.labels.length} total={result.totalCount} name="labels">
      {result.labels.map((label) =>
        typeof label === "string" ? (
          <Text key={label} size="sm">
            {label}
          </Text>
        ) : (
          <GitHubItem key={label.name} title={label.name} info={[label.description]} />
        )
      )}
    </ResultRows>
  )),
});

const readList = repoInput.extend({
  state: z.enum(["OPEN", "CLOSED"]).optional(),
  labels: z.array(z.string()).optional(),
  direction: z.enum(["ASC", "DESC"]).optional(),
  orderBy: z.enum(["CREATED_AT", "UPDATED_AT", "COMMENTS"]).optional(),
  since: z.string().optional(),
  after: z.string().optional(),
  perPage: pageInput.perPage,
  fields: z.array(z.string()).optional(),
  field_filters: z.array(z.strictObject({ field_name: z.string(), value: z.string() })).optional(),
});
const projectedIssues = z.array(issue);
const listResult = z.object({
  issues: projectedIssues,
  totalCount: z.int().optional(),
  pageInfo: pagination.optional(),
});
const issueSearchArgs = z.strictObject({
  ...pageInput,
  query: z.string().min(1),
  owner: z.string().optional(),
  repo: z.string().optional(),
  fields: z.array(z.string()).optional(),
  sort: z.string().optional(),
  order: z.enum(["asc", "desc"]).optional(),
});
const searchResult = z.object({
  items: projectedIssues,
  total_count: z.int().optional(),
  incomplete_results: z.boolean().optional(),
});

const write = repoInput.extend({
  method: z.enum(["create", "update"]),
  issue_number: number.optional(),
  parent_issue_number: number.optional(),
  parent_owner: z.string().optional(),
  parent_repo: z.string().optional(),
  title: z.string().optional(),
  body: z.string().optional(),
  assignees: z.array(z.string()).optional(),
  labels: z.array(z.string()).optional(),
  milestone: z.int().nonnegative().optional(),
  type: z.string().nullish(),
  state: z.enum(["open", "closed"]).optional(),
  state_reason: z.enum(["completed", "not_planned", "duplicate"]).optional(),
  duplicate_of: number.optional(),
  issue_fields: z
    .array(
      z.strictObject({
        field_name: z.string(),
        value: z.union([z.string(), z.number(), z.boolean()]).nullish(),
        field_option_name: z.string().optional(),
        delete: z.boolean().optional(),
      })
    )
    .optional(),
});
// GitHub replaces the complete label set, including when the array is empty.
// Strict parsing prevents compact approval when any other issue field is changed.
const labelWrite = repoInput.extend({
  method: z.literal("update"),
  issue_number: number,
  labels: z.array(z.string().min(1)),
});
const issueWriteResult = z.union([
  issueComment,
  z.object({ method: z.enum(["create", "update"]), issue: issueComment }),
  z.object({ status: z.literal("awaiting_user_submission") }),
]);

function IssueWriteReceipt({ result, context }: ResultPreviewProps<z.infer<typeof issueWriteResult>>): JSX.Element {
  if ("status" in result) return <Text size="sm">GitHub is waiting for the form to be submitted.</Text>;
  const args = write.safeParse(context?.arguments);
  const method = "method" in result ? result.method : args.success ? args.data.method : undefined;
  const item = "issue" in result ? result.issue : result;
  return (
    <Stack gap={4}>
      <Text size="sm">{method === "create" ? "Issue created." : "Issue updated."}</Text>
      <GitHubItem
        title={(args.success && args.data.title) || "Open issue in GitHub"}
        href={item.html_url ?? item.url}
      />
    </Stack>
  );
}

const relation = target.extend({
  method: z.enum(["add", "remove", "reprioritize"]),
  sub_issue_id: number,
  after_id: number.optional(),
  before_id: number.optional(),
  replace_parent: z.boolean().optional(),
});
const relationLabels = { add: "Link sub-issue", remove: "Unlink sub-issue", reprioritize: "Reorder sub-issue" };
const relationResult = z.union([issue, z.object({ method: relation.shape.method, issue: issue.nullable() }), z.null()]);
const addComment = target.extend({
  body: z.string().optional(),
  comment_id: number.optional(),
  reaction: z.enum(["+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"]).optional(),
});
const editComment = repoInput.extend({ comment_id: number, body: z.string() });
const commentReply = z.union([issueComment, z.object({ comment: issueComment, reaction: issueComment })]);
const metaTarget = z.strictObject({ owner: z.string(), repo: z.string().optional() });
const issueTypes = z
  .array(z.object({ name: z.string(), description: z.string().nullish(), is_enabled: z.boolean().optional() }))
  .nullable();
const issueFields = z
  .array(
    z.object({
      name: z.string(),
      description: z.string().nullish(),
      data_type: z.string(),
      visibility: z.string().optional(),
      options: z.array(z.object({ name: z.string() })).nullish(),
    })
  )
  .nullable();

function MetaTarget({ args }: { args: z.infer<typeof metaTarget> }): JSX.Element {
  return args.repo ? (
    <RepoTarget repo={{ ...args, repo: args.repo }} />
  ) : (
    <Field name="Organization" value={args.owner} />
  );
}

function IssueWriteLabel({
  args,
  requestId,
}: {
  args: z.infer<typeof write>;
  requestId?: string;
}): JSX.Element | string {
  const parsed = labelWrite.safeParse(args);
  const summaries = useQuery({
    queryKey: ["action-github-label-summaries", requestId],
    queryFn: ({ signal }) => getGitHubLabelSummaries(requestId ?? "", signal),
    enabled: Boolean(requestId && parsed.success && parsed.data.labels.length),
    retry: false,
    staleTime: 30_000,
  }).data;
  if (!parsed.success) return "Change GitHub issue";
  const target = `${parsed.data.owner}/${parsed.data.repo}#${parsed.data.issue_number}`;
  if (parsed.data.labels.length === 0) return `Clear labels on ${target}`;
  return (
    <span>
      Set labels on {target}:{" "}
      {parsed.data.labels.map((label, index) => {
        const summaryColor = summaries?.labels.find((entry) => entry.name === label)?.color;
        const color = summaryColor && /^[0-9a-fA-F]{6}$/.test(summaryColor) ? `#${summaryColor}` : undefined;
        return (
          <Badge
            key={index}
            component="span"
            color={color ?? "gray.7"}
            c={color && isLightColor(color) ? "black" : "white"}
            size="sm"
            tt="none"
            h="auto"
            maw="100%"
            me={4}
            my={1}
            style={{ verticalAlign: "middle" }}
            styles={{ label: { whiteSpace: "normal", overflowWrap: "anywhere" } }}
          >
            {label}
          </Badge>
        );
      })}
    </span>
  );
}

function IssueWriteArguments({ args }: { args: z.infer<typeof write> }): JSX.Element {
  return (
    <Stack gap={4}>
      {args.issue_number ? (
        <IssueTarget args={{ ...args, issue_number: args.issue_number }} />
      ) : (
        <RepoTarget repo={args} path="/issues" />
      )}
      <Field name={args.method === "create" ? "Create issue" : "Update issue"} value={args.title} />
      <Field name="State" value={displayState(args.state)} />
      <Field name="Reason" value={displayState(args.state_reason)} />
      <Field name="Assignees" value={args.assignees?.length === 0 ? "Clear assignees" : args.assignees?.join(", ")} />
      <Field name="Labels" value={args.labels?.length === 0 ? "Clear labels" : args.labels?.join(", ")} />
      <Field name="Milestone" value={args.milestone === 0 ? "Clear milestone" : args.milestone} />
      <Field name="Issue type" value={args.type === null || args.type === "" ? "Clear type" : args.type} />
      {args.parent_issue_number && (
        <Field
          name="Parent"
          value={
            <GitHubLink
              href={issueHref(
                { owner: args.parent_owner ?? args.owner, repo: args.parent_repo ?? args.repo },
                args.parent_issue_number
              )}
            >
              {args.parent_owner ?? args.owner}/{args.parent_repo ?? args.repo}#{args.parent_issue_number}
            </GitHubLink>
          }
        />
      )}
      {args.duplicate_of && (
        <Field
          name="Duplicate of"
          value={<GitHubLink href={issueHref(args, args.duplicate_of)}>Issue #{args.duplicate_of}</GitHubLink>}
        />
      )}
      {args.issue_fields?.map((field, index) => (
        <Field
          key={index}
          name={field.field_name}
          value={
            field.delete
              ? "Clear value"
              : (field.field_option_name ?? (field.value == null ? undefined : String(field.value)))
          }
        />
      ))}
      {args.body === "" ? (
        <Text size="sm">Clear the description.</Text>
      ) : (
        args.body && <TextBlock text={args.body} heading="Description" format="prose" />
      )}
    </Stack>
  );
}

export const issueRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "issue_read",
    githubPresentation({
      label: "Read GitHub issue",
      schema: read,
      result: issueReadResult,
      compact: ({ args }) => <Text size="sm">{readMethods[args.method]}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          <IssueTarget args={args} />
          <Field name="Read" value={readMethods[args.method]} />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
    }),
  ],
  [
    "list_issues",
    githubPresentation({
      label: "List GitHub issues",
      schema: readList,
      compact: RepoSlug,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/issues" />
          <Field name="State" value={args.state ? displayState(args.state) : "Open and closed"} />
          <Field name="Labels" value={args.labels?.join(", ")} />
          <Field name="Updated since" value={args.since} />
          <Field name="Page size" value={args.perPage} />
          {args.after && <Text size="sm">Continues a previous page.</Text>}
          {args.field_filters?.length ? (
            <Stack gap={4}>
              <Text size="sm" fw={600}>
                Issue field filters
              </Text>
              {args.field_filters.map((filter, i) => (
                <Field key={i} name={filter.field_name} value={filter.value} />
              ))}
            </Stack>
          ) : null}
        </Stack>
      ),
      result: defineResultPreview(listResult, ({ result }) => (
        <ResultRows
          count={result.issues.length}
          total={result.totalCount}
          more={result.pageInfo?.hasNextPage}
          name="issues"
        >
          {result.issues.map((item, index) => (
            <IssueCard key={item.html_url ?? index} item={item} />
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "search_issues",
    githubPresentation({
      label: "Search GitHub issues",
      schema: issueSearchArgs,
      request: ({ args }) => (
        <Stack gap={4}>
          <Field name="Query" value={args.query} />
          {args.owner && args.repo && !/(^|\W)repo:\S+/.test(args.query) && (
            <Field name="Repository" value={`${args.owner}/${args.repo}`} />
          )}
          <Field name="Results per page" value={args.perPage} />
        </Stack>
      ),
      compact: ({ args }) => <Field name="Query" value={args.query} />,
      result: defineResultPreview(searchResult, ({ result }) => (
        <ResultRows
          count={result.items.length}
          total={result.total_count}
          incomplete={result.incomplete_results}
          name="issues"
        >
          {result.items.map((item, index) => (
            <IssueCard key={item.html_url ?? index} item={item} />
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "issue_write",
    {
      ...githubPresentation({
        label: "Change GitHub issue",
        schema: write,
        outcome: awaitingGitHubSubmission,
        compact: ({ args }) => <Text size="sm">{args.method === "create" ? "Create issue" : "Update issue"}</Text>,
        request: ({ args }) => <IssueWriteArguments args={args} />,
        result: defineResultPreview(issueWriteResult, IssueWriteReceipt),
      }),
      label: definePreview(write, IssueWriteLabel),
      pane: {
        collapsed: definePreview(write, ({ args }) =>
          labelWrite.safeParse(args).success ? null : (
            <Text size="sm">{args.method === "create" ? "Create issue" : "Update issue"}</Text>
          )
        ),
        opened: definePreview(write, ({ args }) => <IssueWriteArguments args={args} />),
      },
      offersCardDecisionButtons: (args) => labelWrite.safeParse(args).success,
      compactCardExpands: (args) => !labelWrite.safeParse(args).success,
    },
  ],
  [
    "sub_issue_write",
    githubPresentation({
      label: "Change GitHub sub-issue relationship",
      schema: relation,
      compact: ({ args }) => <Text size="sm">{relationLabels[args.method]}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          <IssueTarget args={args} />
          <Field name={relationLabels[args.method]} value={args.sub_issue_id} />
          <Field name="Move after" value={args.after_id} />
          <Field name="Move before" value={args.before_id} />
          {args.replace_parent && <Text size="sm">Replaces the current parent if one exists.</Text>}
        </Stack>
      ),
      result: defineResultPreview(relationResult, ({ result, context }) => {
        const args = relation.safeParse(context?.arguments);
        const method = result && "method" in result ? result.method : args.success ? args.data.method : undefined;
        const item = result && "issue" in result ? result.issue : result;
        const label =
          method === "add"
            ? "Sub-issue linked."
            : method === "remove"
              ? "Sub-issue unlinked."
              : "Sub-issue order saved.";
        return (
          <Stack gap={4}>
            <Text size="sm">{label}</Text>
            {item && <IssueCard item={item} />}
          </Stack>
        );
      }),
    }),
  ],
  [
    "add_issue_comment",
    githubPresentation({
      label: "Comment or react on GitHub issue",
      schema: addComment,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo}#{args.issue_number}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <IssueTarget args={args} />
          <Field
            name="Existing comment"
            value={
              args.comment_id ? (
                <GitHubLink href={`${issueHref(args, args.issue_number)}#issuecomment-${args.comment_id}`}>
                  Open comment
                </GitHubLink>
              ) : undefined
            }
          />
          <Field name="Reaction" value={args.reaction} />
          {args.body !== undefined && <TextBlock text={args.body} heading="Comment" format="prose" />}
        </Stack>
      ),
      result: defineResultPreview(commentReply, ({ result, context }) => {
        const args = addComment.safeParse(context?.arguments);
        if ("comment" in result)
          return (
            <GitHubItem title="Comment and reaction posted" href={result.comment.html_url ?? result.comment.url} />
          );
        return (
          <GitHubItem
            title={args.success && args.data.body === undefined ? "Reaction added" : "Comment posted"}
            href={result.html_url ?? result.url}
          />
        );
      }),
    }),
  ],
  [
    "update_issue_comment",
    githubPresentation({
      label: "Edit GitHub issue comment",
      schema: editComment,
      compact: RepoSlug,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} />
          <Field name="Comment ID" value={args.comment_id} />
          <TextBlock text={args.body} heading="New comment text" format="prose" />
        </Stack>
      ),
      result: defineResultPreview(issueComment, ({ result }) => (
        <GitHubItem title="Comment updated" href={result.html_url ?? result.url} />
      )),
    }),
  ],
  [
    "list_issue_fields",
    githubPresentation({
      label: "List GitHub issue fields",
      schema: metaTarget,
      request: ({ args }) => <MetaTarget args={args} />,
      compact: ({ args }) => <Field name="Owner" value={args.owner} />,
      result: defineResultPreview(issueFields, ({ result }) => (
        <ResultRows count={result?.length ?? 0} name="issue fields">
          {result?.map((field) => (
            <GitHubItem
              key={field.name}
              title={field.name}
              info={[displayState(field.data_type), field.description, displayState(field.visibility)]}
            >
              <Field name="Options" value={field.options?.map((option) => option.name).join(", ")} />
            </GitHubItem>
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "list_issue_types",
    githubPresentation({
      label: "List GitHub issue types",
      schema: metaTarget,
      request: ({ args }) => <MetaTarget args={args} />,
      compact: ({ args }) => <Field name="Owner" value={args.owner} />,
      result: defineResultPreview(issueTypes, ({ result }) => (
        <ResultRows count={result?.length ?? 0} name="issue types">
          {result?.map((type) => (
            <GitHubItem
              key={type.name}
              title={type.name}
              info={[type.description, type.is_enabled === false ? "Disabled" : undefined]}
            />
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "get_label",
    githubPresentation({
      label: "Read GitHub issue label",
      schema: repoInput.extend({ name: z.string().min(1) }),
      compact: ({ args }) => <Field name="Label" value={args.name} />,
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} path="/labels" />
          <Field name="Label" value={args.name} />
        </Stack>
      ),
      result: defineResultPreview(namedLabel, ({ result, context }) => {
        const repo = repoInput.loose().safeParse(context?.arguments);
        const href = repo.success
          ? `${repositoryHref(repo.data)}/labels/${encodeURIComponent(result.name)}`
          : undefined;
        return <GitHubItem title={result.name} href={href} info={[result.description]} />;
      }),
    }),
  ],
]);
