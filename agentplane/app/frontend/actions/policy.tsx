import type { JSX } from "react";
import { Alert, Badge, Box, Code, Group, Stack, Table, Text, Title, Tooltip } from "@mantine/core";

import { CodeBlock } from "../code_block";
import { expiry } from "../egress";
import type {
  ActionPolicyBindingView,
  ActionPolicySetView,
  ActionPolicyUnavailable,
  ActionPolicyView,
  BoundPolicyView,
  EffectivePolicyView,
  ReadyConditionView,
} from "./client";

const SET_KIND: Record<ActionPolicyBindingView["kind"], string> = {
  ActionPolicyBinding: "ActionPolicySet",
  PolicyBinding: "PolicySet",
};

const PROVENANCE: Record<ActionPolicyBindingView["provenance"], string> = {
  git: "from git",
  app: "app, at launch",
  operator: "operator",
};

// A badge's label may shrink to an ellipsis, so a table column holding one reserves it no width on
// a phone; the badge's own text width is the floor instead.
const WHOLE_BADGE = { minWidth: "max-content" } as const;

export interface Readiness {
  label: string;
  color: string;
  /** What a hover explains; nothing to explain about a current Ready. */
  hint: string | null;
}

/**
 * The Action Service's verdict on an object's spec, as words. Absent until its informer has judged
 * the object at all; behind the object's generation while an edit is unjudged, which reads as
 * "edited" rather than a stale True.
 */
export function readiness(ready: ReadyConditionView | null | undefined, generation?: number): Readiness {
  if (!ready) return { label: "unjudged", color: "gray", hint: "The Action Service has not judged this object yet" };
  if (ready.status !== "True") return { label: ready.reason, color: "red", hint: ready.message };
  if (generation !== undefined && ready.observed_generation !== generation) {
    return {
      label: "edited",
      color: "orange",
      hint: `Judged at generation ${ready.observed_generation ?? "?"}; the spec is at ${generation}`,
    };
  }
  return { label: "Ready", color: "green", hint: null };
}

function Ready({
  ready,
  generation,
}: {
  ready: ReadyConditionView | null | undefined;
  generation?: number;
}): JSX.Element {
  const { label, color, hint } = readiness(ready, generation);
  const badge = (
    <Badge color={color} style={WHOLE_BADGE}>
      {label}
    </Badge>
  );
  if (!hint) return badge;
  return (
    <Tooltip label={hint} withArrow multiline w={320}>
      {badge}
    </Tooltip>
  );
}

/** A set as a binding names it: present and parsed, present but refused, or missing. */
function PolicySets({ binding }: { binding: ActionPolicyBindingView }): JSX.Element {
  return (
    <Group gap={4}>
      {binding.policy_sets.map((policySet: ActionPolicySetView) =>
        policySet.refused ? (
          <Tooltip key={policySet.name} label={policySet.refused} withArrow multiline w={320}>
            <Badge color="red" variant="light">
              {policySet.name}: invalid
            </Badge>
          </Tooltip>
        ) : (
          <Badge key={policySet.name} variant="light">
            {policySet.name}
          </Badge>
        )
      )}
      {binding.missing_policy_sets.map((name) => (
        <Tooltip key={name} label={`No such ${SET_KIND[binding.kind]} in the namespace`} withArrow>
          <Badge color="red" variant="light">
            {name}?
          </Badge>
        </Tooltip>
      ))}
    </Group>
  );
}

function BindingsTable({ bindings }: { bindings: ActionPolicyBindingView[] }): JSX.Element {
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Binding</Table.Th>
          <Table.Th visibleFrom="sm">Provenance</Table.Th>
          <Table.Th visibleFrom="sm">Expires</Table.Th>
          <Table.Th visibleFrom="sm">Ready</Table.Th>
          <Table.Th visibleFrom="sm">Policy sets</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {bindings.length === 0 && (
          <Table.Tr>
            <Table.Td colSpan={5}>
              <Text size="sm" c="dimmed">
                No binding names this ServiceAccount: every Action it submits waits for the operator.
              </Text>
            </Table.Td>
          </Table.Tr>
        )}
        {bindings.map((binding) => (
          <Table.Tr key={`${binding.kind}/${binding.name}`}>
            <Table.Td>
              <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                {binding.name}
              </Text>
              {/* On a phone the other columns fold under the name. */}
              <Stack gap="xs" hiddenFrom="sm" mt="xs">
                <Group gap="xs">
                  <Ready ready={binding.ready} />
                  <Text size="xs" c="dimmed">
                    {PROVENANCE[binding.provenance]} · expires{" "}
                    {binding.expires_at ? new Date(binding.expires_at).toLocaleString() : "never"}
                  </Text>
                </Group>
                <PolicySets binding={binding} />
              </Stack>
            </Table.Td>
            <Table.Td visibleFrom="sm">
              <Text size="sm">{PROVENANCE[binding.provenance]}</Text>
            </Table.Td>
            <Table.Td visibleFrom="sm">{expiry(binding.expires_at)}</Table.Td>
            <Table.Td visibleFrom="sm">
              <Ready ready={binding.ready} />
            </Table.Td>
            <Table.Td visibleFrom="sm">
              <PolicySets binding={binding} />
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
      <Table.Caption>
        The unexpired bindings whose subject is this ServiceAccount, by namespace and name. A binding is the grant: it
        allows while it exists and its sets parse. kubectl edits them; this page only shows them.
      </Table.Caption>
    </Table>
  );
}

/** Every set a binding names, once each, with the verdict the Action Service wrote on it. */
function SetsTable({ bindings }: { bindings: ActionPolicyBindingView[] }): JSX.Element | null {
  // An ActionPolicySet and a PolicySet may share a name; each is its own row.
  const sets = new Map<string, { kind: ActionPolicyBindingView["kind"]; policySet: ActionPolicySetView }>();
  for (const binding of bindings)
    for (const policySet of binding.policy_sets)
      sets.set(`${binding.kind}/${policySet.name}`, { kind: binding.kind, policySet });
  if (sets.size === 0) return null;
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Policy set</Table.Th>
          <Table.Th>Generation</Table.Th>
          <Table.Th>Ready</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {[...sets].map(([key, { kind, policySet }]) => (
          <Table.Tr key={key}>
            <Table.Td>
              <Text size="sm" fw={600}>
                {policySet.name}
              </Text>
              {policySet.refused && (
                <Text size="xs" c="red" style={{ overflowWrap: "anywhere" }}>
                  {policySet.refused}
                </Text>
              )}
            </Table.Td>
            <Table.Td>
              <Text size="sm">{policySet.generation}</Text>
            </Table.Td>
            <Table.Td>
              {kind === "PolicyBinding" ? (
                // A PolicySet has no status object to judge: parsing is its whole verdict.
                <Badge color={policySet.refused ? "red" : "green"} style={WHOLE_BADGE}>
                  {policySet.refused ? "refused" : "Valid"}
                </Badge>
              ) : (
                <Ready ready={policySet.ready} generation={policySet.generation} />
              )}
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

function actionsText(policy: EffectivePolicyView["policy"]): string {
  return Object.entries(policy.actions)
    .map(([group, names]) => `${group}: ${names.join(", ")}`)
    .join(" · ");
}

/** One policy list as the Action Service walks it: first match wins, in this order. */
function PolicyList({
  title,
  policies,
  empty,
}: {
  title: string;
  policies: EffectivePolicyView[];
  empty: string;
}): JSX.Element {
  return (
    <Stack gap="xs">
      <Title order={5}>{title}</Title>
      {policies.length === 0 ? (
        <Text size="sm" c="dimmed">
          {empty}
        </Text>
      ) : (
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>#</Table.Th>
              <Table.Th>Via</Table.Th>
              <Table.Th>Actions</Table.Th>
              <Table.Th visibleFrom="sm">Arguments</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {policies.map((effective, position) => (
              <Table.Tr key={`${effective.binding}/${effective.policy_set}/${effective.source}/${effective.index}`}>
                <Table.Td>
                  <Text size="sm">{position + 1}</Text>
                </Table.Td>
                <Table.Td>
                  <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                    {effective.binding} · {effective.policy_set}[{effective.index}]
                  </Text>
                  <Text size="xs" c="dimmed">
                    {effective.policy.type}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                    {actionsText(effective.policy)}
                  </Text>
                  {/* On a phone the schema folds under the actions, wrapped so the table keeps
                      to the viewport. */}
                  {effective.policy.type === "argument_schema" && (
                    <Box hiddenFrom="sm" mt="xs">
                      <CodeBlock text={JSON.stringify(effective.policy.argument_schema)} language="json" />
                    </Box>
                  )}
                </Table.Td>
                <Table.Td visibleFrom="sm">
                  {effective.policy.type === "argument_schema" ? (
                    <CodeBlock text={JSON.stringify(effective.policy.argument_schema, null, 1)} language="json" />
                  ) : (
                    <Text size="sm" c="dimmed">
                      any
                    </Text>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

/** Why the frame carries no policy: the service could not be asked, and by whom or what. */
function Unavailable({ failure }: { failure: ActionPolicyUnavailable }): JSX.Element {
  return (
    <Alert color="red" role="alert" title="The Action Service could not be asked">
      <Text size="sm">
        <Code>{failure.code}</Code>
        {failure.upstream && (
          <>
            {" "}
            · {failure.upstream.method} {failure.upstream.url}:{" "}
            {failure.upstream.upstream_status ?? failure.upstream.error_type}
          </>
        )}
      </Text>
    </Alert>
  );
}

/**
 * What the Action Service auto-decides for the ServiceAccount, as the service itself resolves it for that
 * account: the bindings, the sets they name, and the auto-approval list as it evaluates it. What
 * arrives here is the service's answer, or why there is none. Read-only.
 */
export function ActionPolicySection({
  policy,
}: {
  policy: ActionPolicyView | ActionPolicyUnavailable | null;
}): JSX.Element {
  if (!policy) return <></>;
  if ("kind" in policy) return <Unavailable failure={policy} />;
  return (
    <Stack gap="md">
      {!policy.synced && (
        <Alert color="orange" role="alert" title="The Action Service's watch has not synced">
          Nothing auto-decides until it has: every Action from this ServiceAccount waits for the operator, whatever the
          objects say.
        </Alert>
      )}
      <BindingsTable bindings={policy.bindings} />
      <SetsTable bindings={policy.bindings} />
      <PolicyList
        title="Auto-approve if"
        policies={policy.auto_approve_if}
        empty="Nothing: every Action from this ServiceAccount waits for the operator."
      />
    </Stack>
  );
}

/**
 * Every rule of the PolicySets a Sandbox names, whichever reader enforces it. The Action Service
 * view above holds only the action rules, so a set's egress and Kubernetes rules show here.
 */
export function BoundPolicySection({ policy }: { policy: BoundPolicyView | null }): JSX.Element {
  if (!policy) return <></>;
  return (
    <Stack gap="xs">
      <Title order={5}>Policy set rules</Title>
      {policy.missing_policy_sets.length > 0 && (
        <Text size="sm" c="red" role="alert">
          No PolicySet answers to {policy.missing_policy_sets.join(", ")}.
        </Text>
      )}
      {policy.policy_sets.length === 0 && (
        <Text size="sm" c="dimmed">
          This Sandbox names no PolicySets.
        </Text>
      )}
      {policy.policy_sets.map((policySet) => (
        <Stack key={policySet.name} gap={4}>
          <Text size="sm" fw={600}>
            {policySet.name}{" "}
            <Text span size="xs" c="dimmed">
              generation {policySet.generation}
            </Text>
          </Text>
          <Table.ScrollContainer minWidth={480} type="native">
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>#</Table.Th>
                  <Table.Th>Type</Table.Th>
                  <Table.Th>Rule</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {policySet.rules.map((rule) => (
                  <Table.Tr key={rule.number}>
                    <Table.Td>{rule.number}</Table.Td>
                    <Table.Td>
                      <Badge variant="light" style={WHOLE_BADGE}>
                        {rule.type}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <CodeBlock text={JSON.stringify(rule.fields, null, 1)} language="json" />
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Stack>
      ))}
    </Stack>
  );
}
