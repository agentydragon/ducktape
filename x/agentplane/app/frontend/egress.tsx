import { Badge, Button, Group, Stack, Table, Text, Title, Tooltip } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { api, displayableError, type BindingView, type Decision, type PolicyView } from "./client";

const REFRESH_MS = 5000;

const APPROVAL_COLORS: Record<BindingView["approval"], string> = {
  approved: "green",
  pending: "yellow",
  denied: "red",
};

function ApprovalBadge({ binding }: { binding: BindingView }): JSX.Element {
  const detail = binding.approved_by
    ? `${binding.approval} by ${binding.approved_by}${binding.approved_at ? ` at ${new Date(binding.approved_at).toLocaleString()}` : ""}`
    : `${binding.approval}, nobody has decided yet`;
  return (
    <Tooltip label={detail} withArrow>
      <Badge color={APPROVAL_COLORS[binding.approval]}>{binding.approval}</Badge>
    </Tooltip>
  );
}

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
  if (binding.from_git) return "from git";
  return binding.granted_by ? `granted by ${binding.granted_by}` : "unlabelled";
}

function subjects(binding: BindingView): string {
  return binding.subjects
    .map((subject) =>
      subject.sandbox
        ? `sandbox ${subject.sandbox}`
        : Object.entries(subject.match_labels ?? {})
            .map(([key, value]) => `${key}=${value}`)
            .join(", ")
    )
    .join("; ");
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

function BindingActions({
  binding,
  onAct,
}: {
  binding: BindingView;
  onAct: (action: BindingAction) => void;
}): JSX.Element {
  return (
    <Group gap="xs" wrap="nowrap" justify="flex-end">
      {binding.approval !== "approved" && (
        <Button size="compact-xs" variant="light" color="green" onClick={() => onAct("approve")}>
          Approve
        </Button>
      )}
      {binding.approval !== "denied" && (
        <Button size="compact-xs" variant="light" color="orange" onClick={() => onAct("deny")}>
          Deny
        </Button>
      )}
      {binding.from_git ? (
        <Tooltip label="Applied by Flux; remove it in git" withArrow>
          <Button size="compact-xs" variant="light" color="gray" disabled>
            Revoke
          </Button>
        </Tooltip>
      ) : (
        <Button size="compact-xs" variant="light" color="red" onClick={() => onAct("revoke")}>
          Revoke
        </Button>
      )}
    </Group>
  );
}

type BindingAction = "approve" | "deny" | "revoke";

function BindingsTable({
  bindings,
  onAct,
}: {
  bindings: BindingView[];
  onAct: (name: string, action: BindingAction) => void;
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
          <Table.Th visibleFrom="sm">Approval</Table.Th>
          <Table.Th visibleFrom="sm">Expires</Table.Th>
          <Table.Th visibleFrom="sm">Policies</Table.Th>
          <Table.Th visibleFrom="sm">Active</Table.Th>
          <Table.Th />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {bindings.length === 0 && (
          <Table.Tr>
            <Table.Td colSpan={7}>
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
                <Tooltip label={subjects(binding)} withArrow>
                  <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                    {binding.name}
                  </Text>
                </Tooltip>
                {/* On a phone the other columns fold under the name. */}
                <Stack gap="xs" hiddenFrom="sm" mt="xs">
                  <Group gap="xs">
                    <ApprovalBadge binding={binding} />
                    <ActiveBadge binding={binding} />
                  </Group>
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
              <Table.Td visibleFrom="sm">
                <ApprovalBadge binding={binding} />
              </Table.Td>
              <Table.Td visibleFrom="sm">{expiry(binding)}</Table.Td>
              <Table.Td visibleFrom="sm">{policyNames}</Table.Td>
              <Table.Td visibleFrom="sm">
                <ActiveBadge binding={binding} />
              </Table.Td>
              <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                <BindingActions binding={binding} onAct={(action) => onAct(binding.name, action)} />
              </Table.Td>
            </Table.Tr>,
          ];
          if (expanded.has(binding.name)) {
            rows.push(
              <Table.Tr key={`${binding.name}-rules`}>
                <Table.Td colSpan={7}>
                  <PolicySummary policies={binding.policies} missing={binding.missing_policies} />
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

/** What may leave the sandbox and what recently did: its bindings and the proxy's decisions. */
export function EgressSection({ name }: { name: string }): JSX.Element {
  const [bindings, setBindings] = useState<BindingView[] | null>(null);
  const [decisions, setDecisions] = useState<Decision[] | null>(null);
  const [decisionsError, setDecisionsError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const params = { params: { path: { name } } };
    const [rules, recent] = await Promise.all([
      api.GET("/sandboxes/{name}/egress", params),
      api.GET("/sandboxes/{name}/egress/decisions", params),
    ]);
    if (rules.error) setError(displayableError(rules.error));
    else {
      setBindings(rules.data);
      setError(null);
    }
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
    const timer = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  async function act(binding: string, action: BindingAction): Promise<void> {
    const params = { params: { path: { name: binding } } };
    const { error: failure } =
      action === "revoke"
        ? await api.DELETE("/egress/bindings/{name}", params)
        : await api.POST(`/egress/bindings/{name}/${action}`, params);
    if (failure) setError(displayableError(failure));
    await refresh();
  }

  return (
    <Stack gap="xs">
      {error && <Text c="red">{error}</Text>}
      {bindings && <BindingsTable bindings={bindings} onAct={(binding, action) => void act(binding, action)} />}
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
