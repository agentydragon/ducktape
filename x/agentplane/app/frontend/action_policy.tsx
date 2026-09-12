import { Badge, Code, Group, Stack, Table, Text, Title, Tooltip } from "@mantine/core";

import type {
  ActionPolicyBindingView,
  ActionPolicySetView,
  ActionPolicyView,
  EffectivePolicyView,
  ReadyConditionView,
} from "./client";
import { expiry } from "./egress";

const PROVENANCE: Record<ActionPolicyBindingView["provenance"], string> = {
  git: "from git",
  app: "app, at launch",
  operator: "operator",
};

// A badge's label may shrink to an ellipsis, so a table column holding one reserves it no width on
// a phone; the badge's own text width is the floor instead.
const WHOLE_BADGE = { minWidth: "max-content" } as const;

/**
 * The Action Service's verdict on an object's spec. Absent until its informer has judged the object
 * at all; behind the object's generation while an edit is unjudged, which the page says rather
 * than showing a stale True as current.
 */
function Ready({
  ready,
  generation,
}: {
  ready: ReadyConditionView | null | undefined;
  generation?: number;
}): JSX.Element {
  if (!ready) {
    return (
      <Tooltip label="The Action Service has not judged this object yet" withArrow>
        <Badge color="gray" variant="light" style={WHOLE_BADGE}>
          unjudged
        </Badge>
      </Tooltip>
    );
  }
  if (ready.status !== "True") {
    return (
      <Tooltip label={ready.message} withArrow multiline w={320}>
        <Badge color="red" style={WHOLE_BADGE}>
          {ready.reason}
        </Badge>
      </Tooltip>
    );
  }
  if (generation !== undefined && ready.observed_generation !== generation) {
    return (
      <Tooltip
        label={`Judged at generation ${ready.observed_generation ?? "?"}; the spec is at ${generation}`}
        withArrow
      >
        <Badge color="orange" style={WHOLE_BADGE}>
          edited
        </Badge>
      </Tooltip>
    );
  }
  return (
    <Badge color="green" style={WHOLE_BADGE}>
      Ready
    </Badge>
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
        <Tooltip key={name} label="No such ActionPolicySet in the namespace" withArrow>
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
                No binding names this sandbox: every Action it submits waits for the operator.
              </Text>
            </Table.Td>
          </Table.Tr>
        )}
        {bindings.map((binding) => (
          <Table.Tr key={binding.name}>
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
        The unexpired bindings whose subject is this sandbox, by name and UID. A binding is the grant: it allows while
        it exists and its sets parse. kubectl edits them; this page only shows them.
      </Table.Caption>
    </Table>
  );
}

/** Every set a binding names, once each, with the verdict the Action Service wrote on it. */
function SetsTable({ bindings }: { bindings: ActionPolicyBindingView[] }): JSX.Element | null {
  const sets = new Map<string, ActionPolicySetView>();
  for (const binding of bindings) for (const policySet of binding.policy_sets) sets.set(policySet.name, policySet);
  if (sets.size === 0) return null;
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Policy set</Table.Th>
          <Table.Th visibleFrom="sm">Generation</Table.Th>
          <Table.Th>Ready</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {[...sets.values()].map((policySet) => (
          <Table.Tr key={policySet.name}>
            <Table.Td>
              <Text size="sm" fw={600}>
                {policySet.name}
              </Text>
              {policySet.refused && (
                <Text size="xs" c="red" style={{ overflowWrap: "anywhere" }}>
                  {policySet.refused}
                </Text>
              )}
              {/* On a phone the generation folds under the name, leaving the verdict its width. */}
              <Text size="xs" c="dimmed" hiddenFrom="sm">
                generation {policySet.generation}
              </Text>
            </Table.Td>
            <Table.Td visibleFrom="sm">
              <Text size="sm">{policySet.generation}</Text>
            </Table.Td>
            <Table.Td>
              <Ready ready={policySet.ready} generation={policySet.generation} />
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

/** One of the three lists as the Action Service walks it: first match wins, in this order. */
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
              <Table.Tr key={`${effective.binding}/${effective.policy_set}/${effective.index}`}>
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
                    <Code block hiddenFrom="sm" mt="xs" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                      {JSON.stringify(effective.policy.argument_schema)}
                    </Code>
                  )}
                </Table.Td>
                <Table.Td visibleFrom="sm">
                  {effective.policy.type === "argument_schema" ? (
                    <Code block>{JSON.stringify(effective.policy.argument_schema, null, 1)}</Code>
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

/**
 * What the Action Service auto-decides for the sandbox, from its pushed bindings: the bindings,
 * the sets they name, and the three lists as the service evaluates them. Read-only.
 */
export function ActionPolicySection({ policy }: { policy: ActionPolicyView | null }): JSX.Element {
  if (!policy) return <></>;
  return (
    <Stack gap="md">
      <BindingsTable bindings={policy.bindings} />
      <SetsTable bindings={policy.bindings} />
      <PolicyList
        title="Auto-approve if"
        policies={policy.auto_approve_if}
        empty="Nothing: every Action from this sandbox waits for the operator."
      />
      {/* The deny lists are accepted and shown but produce no Decision in this version of the
          Action Service (x/agentplane/action_service/SPEC.md § Action policies). */}
      <PolicyList
        title="Auto-deny if"
        policies={policy.auto_deny_if}
        empty="Nothing. Accepted by the Action Service; not yet enforced."
      />
      <PolicyList
        title="Auto-deny unless"
        policies={policy.auto_deny_unless}
        empty="Nothing. Accepted by the Action Service; not yet enforced."
      />
    </Stack>
  );
}
