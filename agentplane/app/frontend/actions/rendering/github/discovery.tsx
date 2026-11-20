import { Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { TextBlock } from "../../../ui/text_block";
import type { ActionPresentation } from "../presentation";
import { defineResultPreview, type ResultPreviewProps } from "../result_entry";
import {
  displayState,
  Field,
  githubPresentation,
  GitHubItem,
  pageInput,
  RepoTarget,
  repoInput,
  ResultRows,
} from "./display";
import { githubHref, repositoryHref } from "./receipts";

const user = z.object({
  login: z.string().min(1),
  profile_url: z.string().optional(),
  html_url: z.string().optional(),
  name: z.string().nullish(),
  role_name: z.string().optional(),
  details: z
    .object({
      name: z.string().optional(),
      bio: z.string().optional(),
      company: z.string().optional(),
      location: z.string().optional(),
    })
    .nullish(),
});
type User = z.infer<typeof user>;

function profile(login: string): string {
  return `https://github.com/${encodeURIComponent(login)}`;
}

function Person({ result }: ResultPreviewProps<User>): JSX.Element {
  const name = result.details?.name || result.name || result.login;
  const href = githubHref(result.profile_url) ?? githubHref(result.html_url) ?? profile(result.login);
  return (
    <GitHubItem
      title={name}
      href={href}
      info={[
        name !== result.login ? result.login : undefined,
        displayState(result.role_name),
        result.details?.company,
        result.details?.location,
      ]}
    >
      {result.details?.bio && <Text size="sm">{result.details.bio}</Text>}
    </GitHubItem>
  );
}

const searchBase = z.strictObject({
  ...pageInput,
  query: z.string().min(1),
  order: z.enum(["asc", "desc"]).optional(),
  sort: z.string().optional(),
});
const codeSearch = searchBase.extend({ fields: z.array(z.string()).optional() });
const repoSearch = searchBase.extend({ minimal_output: z.boolean().optional() });
type SearchInput = z.infer<typeof searchBase>;
function SearchScope({ args }: { args: SearchInput }): JSX.Element {
  return (
    <Stack gap={4}>
      <Field name="Query" value={args.query} />
      <Field name="Page size" value={args.perPage} />
    </Stack>
  );
}

const searchResult = z.object({ total_count: z.int().optional(), incomplete_results: z.boolean().optional() });
const code = z
  .object({
    name: z.string().optional(),
    path: z.string().optional(),
    repository: z.union([z.string(), z.object({ full_name: z.string().optional() })]).optional(),
    html_url: z.string().optional(),
    text_matches: z.array(z.object({ fragment: z.string().optional() })).nullish(),
  })
  .refine((hit) => Boolean(hit.path || hit.name || hit.html_url));
function codeRepository(hit: z.infer<typeof code>): string | undefined {
  return typeof hit.repository === "string" ? hit.repository : hit.repository?.full_name;
}

function codeHref(hit: z.infer<typeof code>): string | undefined {
  const direct = githubHref(hit.html_url);
  if (direct) return direct;
  const name = codeRepository(hit);
  const segments = hit.path?.split("/");
  if (!name || !segments || segments.some((segment) => !segment || segment === "." || segment === ".."))
    return undefined;
  const match = /^([\w-]+)\/([\w.-]+)$/.exec(name);
  if (!match || match[2] === "." || match[2] === "..") return undefined;
  // Code search indexes default branches. The hit carries a blob SHA, which is not a commit ref.
  return `${repositoryHref({ owner: match[1], repo: match[2] })}/blob/HEAD/${segments.map(encodeURIComponent).join("/")}`;
}
const repo = z.object({
  name: z.string().optional(),
  full_name: z.string().min(1),
  html_url: z.string().optional(),
  language: z.string().nullish(),
  description: z.string().nullish(),
  stargazers_count: z.int().optional(),
  archived: z.boolean().optional(),
  private: z.boolean().optional(),
});
const organization = z.object({
  org: z.string(),
  teams: z.array(z.object({ name: z.string(), slug: z.string(), description: z.string().optional() })),
});
const collaborators = z.object({ items: z.array(user), nextPage: z.int().optional() });
const teamArgs = z.strictObject({ org: z.string().min(1), team_slug: z.string().min(1) });

export const discoveryRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "get_me",
    githubPresentation({
      label: "Read authenticated GitHub profile",
      schema: z.strictObject({}),
      staticCard: true,
      result: defineResultPreview(user, Person),
    }),
  ],
  [
    "get_team_members",
    githubPresentation({
      label: "List GitHub team members",
      schema: teamArgs,
      compact: ({ args }) => (
        <Text size="sm">
          {args.org}/{args.team_slug}
        </Text>
      ),
      request: ({ args }) => (
        <GitHubItem
          title={`${args.org}/${args.team_slug}`}
          href={`https://github.com/orgs/${encodeURIComponent(args.org)}/teams/${encodeURIComponent(args.team_slug)}`}
        />
      ),
      result: defineResultPreview(z.array(z.string()).nullable(), ({ result }) => (
        <ResultRows count={result?.length ?? 0} name="team members">
          {result?.map((login) => (
            <GitHubItem key={login} title={login} href={profile(login)} />
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "get_teams",
    githubPresentation({
      label: "List GitHub teams",
      schema: z.strictObject({ user: z.string().optional() }),
      compact: ({ args }) => <Field name="Member" value={args.user || "Authenticated account"} />,
      request: ({ args }) => <Field name="Member" value={args.user || "Authenticated account"} />,
      staticCard: true,
      result: defineResultPreview(z.array(organization).nullable(), ({ result }) => (
        <ResultRows count={result?.reduce((count, org) => count + org.teams.length, 0) ?? 0} name="teams">
          {result?.map((org) => (
            <Stack key={org.org} gap={4}>
              {org.teams.map((team) => (
                <GitHubItem
                  key={team.slug}
                  title={team.name}
                  href={`https://github.com/orgs/${encodeURIComponent(org.org)}/teams/${encodeURIComponent(team.slug)}`}
                  info={[org.org, team.description]}
                />
              ))}
            </Stack>
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "list_repository_collaborators",
    githubPresentation({
      label: "List GitHub repository collaborators",
      schema: repoInput.extend({ ...pageInput, affiliation: z.enum(["direct", "outside", "all"]).optional() }),
      compact: ({ args }) => (
        <Text size="sm">
          {args.owner}/{args.repo}
        </Text>
      ),
      request: ({ args }) => (
        <Stack gap={4}>
          <RepoTarget repo={args} />
          <Field name="Affiliation" value={displayState(args.affiliation ?? "all")} />
          <Field name="Page size" value={args.perPage} />
        </Stack>
      ),
      result: defineResultPreview(collaborators, ({ result }) => (
        <ResultRows count={result.items.length} more={(result.nextPage ?? 0) > 0} name="collaborators">
          {result.items.map((user) => (
            <Person key={user.login} result={user} />
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "search_code",
    githubPresentation({
      label: "Search GitHub code",
      schema: codeSearch,
      request: SearchScope,
      compact: SearchScope,
      staticCard: true,
      result: defineResultPreview(searchResult.extend({ items: z.array(code) }), ({ result }) => (
        <ResultRows
          count={result.items.length}
          total={result.total_count}
          incomplete={result.incomplete_results}
          name="matching files"
        >
          {result.items.map((hit, index) => (
            <GitHubItem
              key={hit.html_url ?? index}
              href={codeHref(hit)}
              title={hit.path || hit.name || "Matching file"}
              info={[codeRepository(hit)]}
            >
              {hit.text_matches?.map((match, index) =>
                match.fragment ? <TextBlock key={index} text={match.fragment} heading="Matching snippet" /> : null
              )}
            </GitHubItem>
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "search_repositories",
    githubPresentation({
      label: "Search GitHub repositories",
      schema: repoSearch,
      request: SearchScope,
      compact: SearchScope,
      staticCard: true,
      result: defineResultPreview(searchResult.extend({ items: z.array(repo) }), ({ result }) => (
        <ResultRows
          count={result.items.length}
          total={result.total_count}
          incomplete={result.incomplete_results}
          name="repositories"
        >
          {result.items.map((repo) => (
            <GitHubItem
              key={repo.full_name}
              title={repo.full_name}
              href={repo.html_url}
              info={[
                repo.language,
                repo.private ? "Private" : undefined,
                repo.archived ? "Archived" : undefined,
                repo.stargazers_count === undefined ? undefined : `${repo.stargazers_count} stars`,
              ]}
            >
              {repo.description && <Text size="sm">{repo.description}</Text>}
            </GitHubItem>
          ))}
        </ResultRows>
      )),
    }),
  ],
  [
    "search_users",
    githubPresentation({
      label: "Search GitHub users",
      schema: searchBase,
      request: SearchScope,
      compact: SearchScope,
      staticCard: true,
      result: defineResultPreview(searchResult.extend({ items: z.array(user) }), ({ result }) => (
        <ResultRows
          count={result.items.length}
          total={result.total_count}
          incomplete={result.incomplete_results}
          name="users"
        >
          {result.items.map((user) => (
            <Person key={user.login} result={user} />
          ))}
        </ResultRows>
      )),
    }),
  ],
]);
