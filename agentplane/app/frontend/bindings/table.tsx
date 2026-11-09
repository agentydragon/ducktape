import { toJson } from "@bufbuild/protobuf";
import { timestampDate } from "@bufbuild/protobuf/wkt";
import { Badge, Box, Button, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import type { JSX } from "react";
import { useSearchParams } from "react-router";

import { TargetSchema, type Credential, type Target } from "../../../egress/egress_rules_pb";
import { RuleSchema } from "../../../policy/api/v1alpha1/policy_types_pb";
import type { PolicyBinding, PolicySet } from "../../../policy/v1/policy_service_pb";
import { CodeBlock } from "../../../../util/frontend/code_block";
import { MarkGlyph } from "../mark_glyph";
import { BINDING_STATE_MARKS } from "../status_mark";
import { TimeAgo } from "../time_ago";
import { useCredentialMetadata } from "./credential_watch";
import { bindingState } from "./state";

export const BINDING_GRANT_HELP =
  "A binding grants the rules of its policy sets until it is deleted. The Policy Controller deletes expired bindings.";

function provenance(binding: PolicyBinding): "git" | "sandbox" | "identity" | "operator" {
  if ("kustomize.toolkit.fluxcd.io/name" in binding.labels) return "git";
  if (binding.ownerReferences.some((owner) => owner.controller && owner.kind === "Sandbox")) return "sandbox";
  return binding.ownerReferences.some((owner) => owner.kind === "Identity") ? "identity" : "operator";
}
const PROVENANCE = { git: "from git", sandbox: "Sandbox", identity: "identity's own", operator: "operator" } as const;
const NOT_REVOCABLE = {
  git: "Applied by Flux: remove it in the repository, or the next reconcile applies it again.",
  sandbox: "Kept equal to the Sandbox's policy sets by its controller, which would recreate it.",
  identity: "Owned by its identity, and deleted with it.",
} as const;

function targetLabel(target: Target): string {
  const value = toJson(TargetSchema, target);
  if (value === null || typeof value !== "object" || Array.isArray(value)) return "unknown target";
  const method =
    typeof value.method === "string"
      ? value.method
          .replace(/^METHOD_/, "")
          .toLowerCase()
          .replaceAll("_", " ")
      : "unknown method";
  return [method, value.header, value.field, value.scheme]
    .filter((part) => typeof part === "string" && part)
    .join(" · ");
}

function PolicySetRules({
  policySet,
  credentials,
}: {
  policySet: PolicySet;
  credentials: Map<string, Credential> | null;
}): JSX.Element {
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600}>
        {policySet.identity?.name}{" "}
        <Text span size="xs" c="dimmed">
          generation {String(policySet.generation)}
        </Text>
      </Text>
      {(policySet.spec?.rules ?? []).map((rule, index) => (
        <Stack key={index} gap={2} pl="sm">
          <Text size="xs" c="dimmed">
            rules[{index}] · {rule.type}
          </Text>
          {rule.type === "egress" ? (
            <Text size="sm" style={{ overflowWrap: "anywhere" }}>
              {rule.hosts.join(", ")} · {rule.methods.length ? rule.methods.join(" ") : "any method"} ·{" "}
              {rule.paths.length ? rule.paths.join(", ") : "any path"}
              {rule.credentialRef
                ? (() => {
                    const credential = credentials?.get(rule.credentialRef.name);
                    return credential
                      ? ` · credential ${credential.name}: ${credential.description} · ${credential.targets.map(targetLabel).join(", ")}`
                      : ` · credential ${rule.credentialRef.name}: ${credentials ? "not found" : "details unavailable"}`;
                  })()
                : " · no credential"}
            </Text>
          ) : (
            <CodeBlock text={JSON.stringify(toJson(RuleSchema, rule))} language="json" />
          )}
        </Stack>
      ))}
    </Stack>
  );
}

export function BindingsTable({
  bindings,
  policySets,
  onRevoke,
  subjectKind,
}: {
  bindings: PolicyBinding[];
  policySets: PolicySet[];
  onRevoke: ((binding: PolicyBinding) => void) | null;
  /** What the table's bindings name, for its empty state. */
  subjectKind: "ServiceAccount" | "identity";
}): JSX.Element {
  const [params, setParams] = useSearchParams();
  const credentials = useCredentialMetadata();
  const expanded = new Set((params.get("rules") ?? "").split(",").filter(Boolean));
  const sets = new Map(policySets.map((set) => [set.identity?.name ?? "", set]));
  function toggle(name: string): void {
    if (!expanded.delete(name)) expanded.add(name);
    const next = new URLSearchParams(params);
    if (expanded.size) next.set("rules", [...expanded].join(","));
    else next.delete("rules");
    setParams(next, { replace: true });
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
          {onRevoke && <Table.Th />}
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {bindings.length === 0 && (
          <Table.Tr>
            <Table.Td colSpan={columns}>
              <Text size="sm" c="dimmed">
                No binding names this {subjectKind}.
              </Text>
            </Table.Td>
          </Table.Tr>
        )}
        {bindings.flatMap((binding) => {
          const name = binding.identity?.name ?? "";
          const source = provenance(binding);
          const state = bindingState(binding, sets);
          const expires = binding.spec?.expiresAt;
          const expiresAt = expires ? timestampDate(expires) : null;
          const references = binding.spec?.policySets ?? [];
          const labels = (
            <Group gap={4}>
              {references.map((reference) => (
                <Badge key={reference} color={sets.has(reference) ? undefined : "red"} variant="light">
                  {reference}
                  {sets.has(reference) ? "" : "?"}
                </Badge>
              ))}
              <Button size="compact-xs" variant="subtle" onClick={() => toggle(name)}>
                {expanded.has(name) ? "Hide rules" : "Rules"}
              </Button>
            </Group>
          );
          const subject = binding.spec?.subject;
          const rows = [
            <Table.Tr key={name}>
              <Table.Td>
                <Tooltip
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
                  withArrow
                  multiline
                  w={320}
                >
                  <Box component="span" role="img" aria-label={state.title} tabIndex={0}>
                    <MarkGlyph mark={BINDING_STATE_MARKS[state.kind]} size="small" />
                  </Box>
                </Tooltip>
              </Table.Td>
              <Table.Td>
                <Tooltip
                  label={
                    subject?.identity ? `identity ${subject.identity.name}` : `${subject?.namespace}/${subject?.name}`
                  }
                  withArrow
                >
                  <Text size="sm" fw={600}>
                    {name}
                  </Text>
                </Tooltip>
                <Stack gap="xs" hiddenFrom="sm" mt="xs">
                  <Text size="xs" c="dimmed">
                    {PROVENANCE[source]} · expires {expiresAt ? <TimeAgo value={expiresAt} /> : "never"}
                  </Text>
                  {labels}
                </Stack>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Text size="sm">{PROVENANCE[source]}</Text>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Text size="sm" c={expiresAt && expiresAt.getTime() < Date.now() ? "red" : undefined}>
                  {expiresAt ? <TimeAgo value={expiresAt} /> : "never"}
                </Text>
              </Table.Td>
              <Table.Td visibleFrom="sm">{labels}</Table.Td>
              {onRevoke && (
                <Table.Td>
                  {source === "operator" ? (
                    <Button size="compact-xs" color="red" variant="light" onClick={() => onRevoke(binding)}>
                      Revoke
                    </Button>
                  ) : (
                    <Tooltip label={NOT_REVOCABLE[source]}>
                      <Button size="compact-xs" data-disabled onClick={(event) => event.preventDefault()}>
                        Revoke
                      </Button>
                    </Tooltip>
                  )}
                </Table.Td>
              )}
            </Table.Tr>,
          ];
          if (expanded.has(name))
            rows.push(
              <Table.Tr key={`${name}-rules`}>
                <Table.Td colSpan={columns}>
                  <Stack gap="sm">
                    {references.map((reference) => {
                      const set = sets.get(reference);
                      return set ? (
                        <PolicySetRules key={reference} policySet={set} credentials={credentials} />
                      ) : (
                        <Text key={reference} size="sm" c="red">
                          {reference}: not present in the current PolicySet view
                        </Text>
                      );
                    })}
                  </Stack>
                </Table.Td>
              </Table.Tr>
            );
          return rows;
        })}
      </Table.Tbody>
    </Table>
  );
}
