import { Badge, Box, Button, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import type { JSX } from "react";
import { useSearchParams } from "react-router";

import type { BindingView, EgressRuleView, PolicySetView } from "../api/schema";
import { bindingState } from "./state";
import { CodeBlock } from "../../../../util/frontend/code_block";
import { MarkGlyph } from "../mark_glyph";
import { BINDING_STATE_MARKS } from "../status_mark";
import { TimeAgo } from "../time_ago";

const PROVENANCE: Record<BindingView["provenance"], string> = {
  git: "from git",
  sandbox: "Sandbox",
  operator: "operator",
};

/** The bindings whose rules are open, comma-separated; a binding name has no comma in it. */
const RULES_PARAM = "rules";

const REVOKE_EXPLAINS =
  "Deletes the binding, which is what takes the access away. There is no undo; a new binding has to be made.";
const NOT_REVOCABLE: Partial<Record<BindingView["provenance"], string>> = {
  git: "Applied by Flux: remove it in the repository, or the next reconcile applies it again.",
  sandbox: "Kept equal to the Sandbox's policy sets by its controller, which would recreate it.",
};

export const BINDING_GRANT_HELP =
  "A binding grants the rules of its policy sets until it expires or is revoked. One from the repository is removed there.";

/** When a binding stops counting, red once that is past: the objects are pushed, the clock is not. */
function Expiry({ expiresAt }: { expiresAt: string | null | undefined }): JSX.Element {
  if (!expiresAt) return <Text size="sm">never</Text>;
  const at = new Date(expiresAt);
  return (
    <Text size="sm" c={at.getTime() < Date.now() ? "red" : undefined}>
      <TimeAgo value={at} />
    </Text>
  );
}

/** Whether the binding's readers accept it, as an icon; why is on hover. */
function BindingStateMark({ binding }: { binding: BindingView }): JSX.Element {
  const state = bindingState(binding);
  return (
    <Tooltip
      multiline
      w={320}
      withArrow
      events={{ hover: true, focus: true, touch: true }}
      label={
        <Stack gap={2}>
          <Text size="sm" fw={600}>
            {state.title}
          </Text>
          {state.reasons.map((reason) => (
            <Text key={reason} size="xs">
              {reason}
            </Text>
          ))}
        </Stack>
      }
    >
      <Box component="span" role="img" aria-label={state.title} tabIndex={0} style={{ display: "inline-flex" }}>
        <MarkGlyph mark={BINDING_STATE_MARKS[state.kind]} size="small" />
      </Box>
    </Tooltip>
  );
}

function EgressRule({ rule }: { rule: EgressRuleView }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      {rule.hosts.join(", ")} · {rule.methods ? rule.methods.join(" ") : "any method"} ·{" "}
      {rule.paths ? rule.paths.join(", ") : "any path"}
      {rule.credential
        ? ` · ${rule.credential.name}: ${rule.credential.description} (${rule.credential.targets
            .map((t) =>
              t.method === "jsonField"
                ? `JSON field ${t.field}: <credential>`
                : t.method === "schemeToken"
                  ? `${t.header}: ${t.scheme} <credential>`
                  : `${t.header} ${t.method}`
            )
            .join(", ")}, from ${
            rule.credential.secret && rule.credential.key
              ? `${rule.credential.secret}/${rule.credential.key}`
              : "authenticated workload token"
          })`
        : rule.missing_credential
          ? ` · credential ${rule.missing_credential}: no such EgressCredential`
          : " · no credential"}
    </Text>
  );
}

/** A set's rules in its order, each under the type that selects the reader enforcing it. */
function PolicySetRules({ policySet }: { policySet: PolicySetView }): JSX.Element {
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600}>
        {policySet.name}{" "}
        <Text span size="xs" c="dimmed" fw={400}>
          generation {policySet.generation}
        </Text>
      </Text>
      {policySet.refused && (
        <Text size="sm" c="red" style={{ overflowWrap: "anywhere" }}>
          {policySet.refused}
        </Text>
      )}
      {policySet.rules.map((rule) => (
        <Stack key={rule.number} gap={2} pl="sm">
          <Text size="xs" c="dimmed">
            rules[{rule.number}] · {rule.kind === "egress" ? "egress" : rule.type}
          </Text>
          {rule.kind === "egress" ? (
            <EgressRule rule={rule} />
          ) : (
            <CodeBlock text={JSON.stringify(rule.fields)} language="json" />
          )}
        </Stack>
      ))}
    </Stack>
  );
}

function RevokeButton({ binding, onRevoke }: { binding: BindingView; onRevoke: () => void }): JSX.Element {
  const unavailable = NOT_REVOCABLE[binding.provenance];
  return (
    <Tooltip label={unavailable ?? REVOKE_EXPLAINS} withArrow multiline w={280}>
      {unavailable ? (
        // `disabled` fires no pointer events, so the button would carry a tooltip nobody sees.
        <Button
          size="compact-xs"
          variant="light"
          color="gray"
          data-disabled
          onClick={(event) => event.preventDefault()}
        >
          Revoke
        </Button>
      ) : (
        <Button size="compact-xs" variant="light" color="red" onClick={onRevoke}>
          Revoke
        </Button>
      )}
    </Tooltip>
  );
}

/**
 * The bindings naming a ServiceAccount: what each is, when it ends, its sets and every rule in them, and
 * whether its readers accept it. `onRevoke` is null where the list is only shown.
 */
export function BindingsTable({
  bindings,
  onRevoke,
}: {
  bindings: BindingView[];
  onRevoke: ((name: string) => void) | null;
}): JSX.Element {
  // Which bindings show their rules is in the URL, like the tab and the session page's switches, so
  // a reading of what a sandbox may reach can be linked to and survives a reload.
  const [searchParams, setSearchParams] = useSearchParams();
  const expanded = new Set((searchParams.get(RULES_PARAM) ?? "").split(",").filter((name) => name));
  function toggle(name: string): void {
    const next = new Set(expanded);
    if (!next.delete(name)) next.add(name);
    const params = new URLSearchParams(searchParams);
    if (next.size === 0) params.delete(RULES_PARAM);
    else params.set(RULES_PARAM, [...next].join(","));
    setSearchParams(params, { replace: true });
  }
  const columns = onRevoke === null ? 5 : 6;
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th />
          <Table.Th>Binding</Table.Th>
          <Table.Th visibleFrom="sm">Provenance</Table.Th>
          <Table.Th visibleFrom="sm">Expires</Table.Th>
          <Table.Th visibleFrom="sm">Policy sets</Table.Th>
          {onRevoke !== null && <Table.Th />}
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {bindings.length === 0 && (
          <Table.Tr>
            <Table.Td colSpan={columns}>
              <Text size="sm" c="dimmed">
                No binding names this ServiceAccount.
              </Text>
            </Table.Td>
          </Table.Tr>
        )}
        {bindings.flatMap((binding) => {
          const policySets = (
            <Group gap={4}>
              {binding.policy_sets.map((policySet) => (
                <Tooltip
                  key={policySet.name}
                  label={policySet.refused}
                  disabled={!policySet.refused}
                  withArrow
                  multiline
                  w={320}
                >
                  <Badge color={policySet.refused ? "red" : undefined} variant="light">
                    {policySet.name}
                  </Badge>
                </Tooltip>
              ))}
              {binding.missing_policy_sets.map((name) => (
                <Tooltip key={name} label="No such PolicySet in the namespace" withArrow>
                  <Badge color="red" variant="light">
                    {name}?
                  </Badge>
                </Tooltip>
              ))}
              <Button size="compact-xs" variant="subtle" onClick={() => toggle(binding.name)}>
                {expanded.has(binding.name) ? "Hide rules" : "Rules"}
              </Button>
            </Group>
          );
          const rows = [
            <Table.Tr key={binding.name}>
              <Table.Td style={{ width: "1%" }}>
                <BindingStateMark binding={binding} />
              </Table.Td>
              <Table.Td>
                <Tooltip
                  label={binding.subjects.map((subject) => `${subject.namespace}/${subject.name}`).join(", ")}
                  withArrow
                >
                  <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                    {binding.name}
                  </Text>
                </Tooltip>
                {/* On a phone the other columns fold under the name. */}
                <Stack gap="xs" hiddenFrom="sm" mt="xs">
                  <Text size="xs" c="dimmed">
                    {PROVENANCE[binding.provenance]} · expires{" "}
                    {binding.expires_at ? <TimeAgo value={binding.expires_at} /> : "never"}
                  </Text>
                  {policySets}
                </Stack>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Text size="sm">{PROVENANCE[binding.provenance]}</Text>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Expiry expiresAt={binding.expires_at} />
              </Table.Td>
              <Table.Td visibleFrom="sm">{policySets}</Table.Td>
              {onRevoke !== null && (
                <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  <Group gap="xs" wrap="nowrap" justify="flex-end">
                    <RevokeButton binding={binding} onRevoke={() => onRevoke(binding.name)} />
                  </Group>
                </Table.Td>
              )}
            </Table.Tr>,
          ];
          if (expanded.has(binding.name)) {
            rows.push(
              <Table.Tr key={`${binding.name}-rules`}>
                <Table.Td colSpan={columns}>
                  <Stack gap="sm">
                    {binding.policy_sets.map((policySet) => (
                      <PolicySetRules key={policySet.name} policySet={policySet} />
                    ))}
                    {binding.missing_policy_sets.map((name) => (
                      <Text key={name} size="sm" c="red">
                        {name}: no such PolicySet
                      </Text>
                    ))}
                  </Stack>
                </Table.Td>
              </Table.Tr>
            );
          }
          return rows;
        })}
      </Table.Tbody>
    </Table>
  );
}
