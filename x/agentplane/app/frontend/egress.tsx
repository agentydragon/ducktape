import { Badge, Button, Group, MultiSelect, Stack, Table, Text, Title, Tooltip } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { api, displayableError, type BindingView, type Decision, type PolicyView } from "./client";

// The proxy keeps its recent decisions in memory and offers no stream, so this one view still
// asks. Everything else on the page is pushed (live.tsx).
const DECISIONS_REFRESH_MS = 5000;

/** The proxy's Active condition as written; grey until the proxy has looked at the binding. */
function ActiveBadge({ binding }: { binding: BindingView }): JSX.Element {
  if (binding.active === null) return <Badge color="gray">unknown</Badge>;
  const label = [binding.active_reason, binding.active_message].filter((part) => part).join(": ");
  return (
    <Tooltip label={label || (binding.active ? "Active" : "Inactive")} withArrow>
      <Badge color={binding.active ? "green" : "orange"}>{binding.active ? "active" : "inactive"}</Badge>
    </Tooltip>
  );
}

function provenance(binding: BindingView): string {
  return binding.from_git ? "from git" : "runtime";
}

function expiry(binding: BindingView): JSX.Element {
  if (!binding.expires_at) return <Text size="sm">never</Text>;
  const at = new Date(binding.expires_at);
  return (
    <Text size="sm" c={at.getTime() < Date.now() ? "red" : undefined}>
      {at.toLocaleString()}
    </Text>
  );
}

/** Every rule of every resolved policy, one line each: what may be reached and with which credential. */
function PolicySummary({ policies, missing }: { policies: PolicyView[]; missing: string[] }): JSX.Element {
  return (
    <Stack gap="xs">
      {policies.map((policy) => (
        <Stack key={policy.name} gap={2}>
          <Text size="sm" fw={600}>
            {policy.name}
          </Text>
          {policy.rules.map((rule, index) => (
            <Text key={index} size="sm" style={{ overflowWrap: "anywhere" }}>
              {rule.hosts.join(", ")} · {rule.methods ? rule.methods.join(" ") : "any method"} ·{" "}
              {rule.paths ? rule.paths.join(", ") : "any path"}
              {rule.credential
                ? ` · ${rule.credential.header} from ${rule.credential.secret}/${rule.credential.key}`
                : " · no credential"}
            </Text>
          ))}
        </Stack>
      ))}
      {missing.map((name) => (
        <Text key={name} size="sm" c="red">
          {name}: no such policy
        </Text>
      ))}
    </Stack>
  );
}

const REVOKE_EXPLAINS =
  "Deletes the rule, which is what takes the access away. There is no undo; a new binding has to be made.";
const REVOKE_FROM_GIT = "Applied by Flux: remove it in the repository, or the next reconcile applies it again.";

function BindingActions({ binding, onRevoke }: { binding: BindingView; onRevoke: () => void }): JSX.Element {
  return (
    <Group gap="xs" wrap="nowrap" justify="flex-end">
      <Tooltip label={binding.from_git ? REVOKE_FROM_GIT : REVOKE_EXPLAINS} withArrow multiline w={280}>
        {binding.from_git ? (
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
    </Group>
  );
}

function BindingsTable({
  bindings,
  onRevoke,
}: {
  bindings: BindingView[];
  onRevoke: (name: string) => void;
}): JSX.Element {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  function toggle(name: string): void {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(name)) next.add(name);
      return next;
    });
  }
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Binding</Table.Th>
          <Table.Th visibleFrom="sm">Provenance</Table.Th>
          <Table.Th visibleFrom="sm">Expires</Table.Th>
          <Table.Th visibleFrom="sm">Policies</Table.Th>
          <Table.Th visibleFrom="sm">Active</Table.Th>
          <Table.Th />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {bindings.length === 0 && (
          <Table.Tr>
            <Table.Td colSpan={6}>
              <Text size="sm" c="dimmed">
                No binding names this sandbox: nothing may leave it.
              </Text>
            </Table.Td>
          </Table.Tr>
        )}
        {bindings.flatMap((binding) => {
          const policyNames = (
            <Group gap={4}>
              {binding.policies.map((policy) => (
                <Badge key={policy.name} variant="light">
                  {policy.name}
                </Badge>
              ))}
              {binding.missing_policies.map((name) => (
                <Badge key={name} color="red" variant="light">
                  {name}?
                </Badge>
              ))}
              <Button size="compact-xs" variant="subtle" onClick={() => toggle(binding.name)}>
                {expanded.has(binding.name) ? "Hide rules" : "Rules"}
              </Button>
            </Group>
          );
          const rows = [
            <Table.Tr key={binding.name}>
              <Table.Td>
                <Tooltip label={binding.subjects.join(", ")} withArrow>
                  <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                    {binding.name}
                  </Text>
                </Tooltip>
                {/* On a phone the other columns fold under the name. */}
                <Stack gap="xs" hiddenFrom="sm" mt="xs">
                  <ActiveBadge binding={binding} />
                  <Text size="xs" c="dimmed">
                    {provenance(binding)} · expires{" "}
                    {binding.expires_at ? new Date(binding.expires_at).toLocaleString() : "never"}
                  </Text>
                  {policyNames}
                </Stack>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Text size="sm">{provenance(binding)}</Text>
              </Table.Td>
              <Table.Td visibleFrom="sm">{expiry(binding)}</Table.Td>
              <Table.Td visibleFrom="sm">{policyNames}</Table.Td>
              <Table.Td visibleFrom="sm">
                <ActiveBadge binding={binding} />
              </Table.Td>
              <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                <BindingActions binding={binding} onRevoke={() => onRevoke(binding.name)} />
              </Table.Td>
            </Table.Tr>,
          ];
          if (expanded.has(binding.name)) {
            rows.push(
              <Table.Tr key={`${binding.name}-rules`}>
                <Table.Td colSpan={6}>
                  <PolicySummary policies={binding.policies} missing={binding.missing_policies} />
                </Table.Td>
              </Table.Tr>
            );
          }
          return rows;
        })}
      </Table.Tbody>
      <Table.Caption>
        A binding is the permission: it allows while it exists, and revoking deletes it. One from the repository is
        removed there.
      </Table.Caption>
    </Table>
  );
}

/** Grants to a sandbox that is already running; each grant is its own binding, revoked on its own. */
function GrantPolicies({
  policies,
  picked,
  onPick,
  onGrant,
}: {
  policies: string[];
  picked: string[];
  onPick: (names: string[]) => void;
  onGrant: () => void;
}): JSX.Element {
  return (
    <Group align="flex-end">
      <MultiSelect
        label="Grant policies"
        description="Added as a binding of its own; what this sandbox already has is untouched"
        data={policies}
        value={picked}
        onChange={onPick}
        style={{ flex: "1 1 12rem" }}
      />
      <Button onClick={onGrant} disabled={picked.length === 0}>
        Grant
      </Button>
    </Group>
  );
}

function DecisionsTable({ decisions }: { decisions: Decision[] }): JSX.Element {
  // Newest first: the ring is served oldest first.
  const rows = [...decisions].reverse();
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Time</Table.Th>
          <Table.Th>Method</Table.Th>
          <Table.Th>Host</Table.Th>
          <Table.Th visibleFrom="sm">Path</Table.Th>
          <Table.Th>Decision</Table.Th>
          <Table.Th visibleFrom="sm">Reason</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {rows.length === 0 && (
          <Table.Tr>
            <Table.Td colSpan={6}>
              <Text size="sm" c="dimmed">
                No decisions yet.
              </Text>
            </Table.Td>
          </Table.Tr>
        )}
        {rows.map((decision, index) => {
          const reason =
            decision.outcome === "allow"
              ? [decision.policy, decision.substituted ? "credential substituted" : null]
                  .filter((part) => part)
                  .join(" · ")
              : (decision.reason ?? "");
          return (
            <Table.Tr key={index}>
              <Table.Td>
                <Text size="sm" style={{ whiteSpace: "nowrap" }}>
                  {new Date(decision.at).toLocaleTimeString()}
                </Text>
              </Table.Td>
              <Table.Td>
                <Text size="sm">{decision.method}</Text>
              </Table.Td>
              <Table.Td>
                <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                  {decision.host}
                  {decision.port !== 443 ? `:${decision.port}` : ""}
                </Text>
                {decision.address && (
                  <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                    {decision.address}
                  </Text>
                )}
                <Text size="xs" c="dimmed" hiddenFrom="sm" style={{ overflowWrap: "anywhere" }}>
                  {[decision.path, reason].filter((part) => part).join(" · ")}
                </Text>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                  {decision.path ?? "—"}
                </Text>
              </Table.Td>
              <Table.Td>
                <Badge color={decision.outcome === "allow" ? "green" : "red"}>{decision.outcome}</Badge>
              </Table.Td>
              <Table.Td visibleFrom="sm">
                <Text size="sm">{reason || "—"}</Text>
              </Table.Td>
            </Table.Tr>
          );
        })}
      </Table.Tbody>
    </Table>
  );
}

/** What may leave the sandbox and what recently did: its pushed bindings and the proxy's decisions. */
export function EgressSection({ name, bindings }: { name: string; bindings: BindingView[] | null }): JSX.Element {
  const [decisions, setDecisions] = useState<Decision[] | null>(null);
  const [decisionsError, setDecisionsError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The namespace's policies, and the ones picked to grant this sandbox next.
  const [policies, setPolicies] = useState<string[]>([]);
  const [picked, setPicked] = useState<string[]>([]);

  const refresh = useCallback(async () => {
    const recent = await api.GET("/sandboxes/{name}/egress/decisions", { params: { path: { name } } });
    if (recent.error) {
      setDecisions(null);
      setDecisionsError(displayableError(recent.error));
    } else {
      setDecisions(recent.data);
      setDecisionsError(null);
    }
  }, [name]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), DECISIONS_REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await api.GET("/egress/policies");
      setError(failure ? displayableError(failure) : null);
      if (!failure) setPolicies(data.map((policy) => policy.name));
    })();
  }, []);

  // What the API server now holds, granted or revoked, arrives on the page's stream; nothing to
  // re-read here.
  async function revoke(binding: string): Promise<void> {
    const { error: failure } = await api.DELETE("/egress/bindings/{name}", {
      params: { path: { name: binding } },
    });
    setError(failure ? displayableError(failure) : null);
  }

  async function grant(): Promise<void> {
    const { error: failure } = await api.POST("/sandboxes/{name}/egress", {
      params: { path: { name } },
      body: { policies: picked },
    });
    setError(failure ? displayableError(failure) : null);
    if (!failure) setPicked([]);
  }

  return (
    <Stack gap="xs">
      {error && <Text c="red">{error}</Text>}
      <GrantPolicies policies={policies} picked={picked} onPick={setPicked} onGrant={() => void grant()} />
      {bindings && <BindingsTable bindings={bindings} onRevoke={(binding) => void revoke(binding)} />}
      <Title order={5}>Recent decisions</Title>
      {decisions && <DecisionsTable decisions={decisions} />}
      {decisionsError && (
        <Text size="sm" c="dimmed">
          The egress proxy could not be asked; the rules above still apply. ({decisionsError})
        </Text>
      )}
    </Stack>
  );
}
