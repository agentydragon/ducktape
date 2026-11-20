import { Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreview } from "../result_entry";
import {
  byMethod,
  displayState,
  Field,
  githubPresentation,
  GitHubItem,
  RepoTarget,
  repoInput,
  ResultRows,
} from "./display";

const uiMethods = {
  labels: "Labels",
  assignees: "Assignees",
  milestones: "Milestones",
  issue_types: "Issue types",
  branches: "Branches",
  issue_fields: "Issue fields",
  reviewers: "Reviewers",
} as const;
const uiArgs = z.strictObject({
  owner: z.string().min(1),
  repo: z.string().optional(),
  method: z.enum(Object.keys(uiMethods) as [keyof typeof uiMethods, ...(keyof typeof uiMethods)[]]),
});
const named = z.object({ name: z.string(), description: z.string().nullish(), is_enabled: z.boolean().optional() });
const members = z.object({ login: z.string() });
const summary = z.object({ totalCount: z.int().optional(), has_more: z.boolean().optional() });
const milestone = z.object({
  title: z.string(),
  state: z.string().optional(),
  description: z.string().nullish(),
  due_on: z.string().nullish(),
});
const branch = z.object({ name: z.string(), protected: z.boolean().optional() });
const issueField = named.extend({ data_type: z.string().optional(), options: z.array(named).nullish() });
const team = z.object({ name: z.string(), slug: z.string(), org: z.string() });

function uiResult<S extends z.ZodType>(
  method: keyof typeof uiMethods,
  schema: S,
  render: (item: z.infer<S>) => JSX.Element
): ResultPreview {
  const envelope = z.object({ method: z.literal(method) }).catchall(z.unknown());
  return defineResultPreview(
    z.preprocess((value) => {
      const parsed = envelope.safeParse(value);
      return parsed.success ? parsed.data[method] : value;
    }, schema),
    ({ result }) => render(result)
  );
}

const uiResults = byMethod({
  labels: uiResult("labels", summary.extend({ labels: z.array(named) }), (result) => (
    <ResultRows count={result.labels.length} total={result.totalCount} more={result.has_more} name="labels">
      {result.labels.map((item) => (
        <GitHubItem key={item.name} title={item.name} info={[item.description]} />
      ))}
    </ResultRows>
  )),
  assignees: uiResult("assignees", summary.extend({ assignees: z.array(members) }), (result) => (
    <ResultRows count={result.assignees.length} total={result.totalCount} more={result.has_more} name="assignees">
      {result.assignees.map(({ login }) => (
        <GitHubItem key={login} title={login} href={`https://github.com/${encodeURIComponent(login)}`} />
      ))}
    </ResultRows>
  )),
  milestones: uiResult("milestones", summary.extend({ milestones: z.array(milestone) }), (result) => (
    <ResultRows count={result.milestones.length} total={result.totalCount} more={result.has_more} name="milestones">
      {result.milestones.map((item, index) => (
        <GitHubItem key={index} title={item.title} info={[displayState(item.state), item.description, item.due_on]} />
      ))}
    </ResultRows>
  )),
  issue_types: uiResult("issue_types", z.array(named).nullable(), (result) => (
    <ResultRows count={result?.length ?? 0} name="issue types">
      {result?.map((item) => (
        <GitHubItem
          key={item.name}
          title={item.name}
          info={[item.description, item.is_enabled === false ? "Disabled" : undefined]}
        />
      ))}
    </ResultRows>
  )),
  branches: uiResult("branches", summary.extend({ branches: z.array(branch) }), (result) => (
    <ResultRows count={result.branches.length} total={result.totalCount} more={result.has_more} name="branches">
      {result.branches.map((item) => (
        <GitHubItem key={item.name} title={item.name} info={[item.protected ? "Protected" : undefined]} />
      ))}
    </ResultRows>
  )),
  issue_fields: uiResult("issue_fields", summary.extend({ fields: z.array(issueField) }), (result) => (
    <ResultRows count={result.fields.length} total={result.totalCount} name="issue fields">
      {result.fields.map((item) => (
        <GitHubItem key={item.name} title={item.name} info={[displayState(item.data_type), item.description]}>
          <Field name="Options" value={item.options?.map((option) => option.name).join(", ")} />
        </GitHubItem>
      ))}
    </ResultRows>
  )),
  reviewers: uiResult("reviewers", summary.extend({ users: z.array(members), teams: z.array(team) }), (result) => (
    <ResultRows
      count={result.users.length + result.teams.length}
      total={result.totalCount}
      more={result.has_more}
      name="reviewers"
    >
      {result.users.map(({ login }) => (
        <GitHubItem key={login} title={login} href={`https://github.com/${encodeURIComponent(login)}`} />
      ))}
      {result.teams.map((team) => (
        <GitHubItem
          key={`${team.org}/${team.slug}`}
          title={team.name}
          info={[team.org]}
          href={`https://github.com/orgs/${encodeURIComponent(team.org)}/teams/${encodeURIComponent(team.slug)}`}
        />
      ))}
    </ResultRows>
  )),
});

const scan = repoInput.extend({ files: z.union([z.string().min(1), z.array(z.string()).min(1).max(100)]) });

export const githubMetadataRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "ui_get",
    githubPresentation({
      label: "Read GitHub form options",
      schema: uiArgs,
      result: uiResults,
      compact: ({ args }) => <Text size="sm">{uiMethods[args.method]}</Text>,
      request: ({ args }) => (
        <Stack gap={4}>
          {args.repo ? (
            <RepoTarget repo={{ ...args, repo: args.repo }} />
          ) : (
            <Field name="Organization" value={args.owner} />
          )}
          <Field name="Read" value={uiMethods[args.method]} />
        </Stack>
      ),
    }),
  ],
  [
    "run_secret_scanning",
    githubPresentation({
      label: "Scan for secrets with GitHub",
      schema: scan,
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} />
          <Field
            name="Content submitted"
            value={
              typeof args.files === "string" || args.files.length === 1 ? "One block" : `${args.files.length} blocks`
            }
          />
          <Text size="sm">View submitted content and scan findings in Raw.</Text>
        </Stack>
      ),
      // Hosted-only scanner results may include credential values. The result contract isn't public.
      result: null,
    }),
  ],
]);
