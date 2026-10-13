import { Badge, Button, Group, MultiSelect, SegmentedControl, Stack, Table, Text, Title, Tooltip } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import { api, displayableError, type BindingView, type Decision, type PolicyView } from "./client";
import { type DecisionGroup, groupDecisions } from "./decision_groups";
import { type DecisionsSnapshot, liveEgressDecisionsUrl } from "./live";
import { followStream, type StreamConnection } from "./live_stream";
import { StaleNotice, type StreamStatus, useStreamStatus } from "./stream_status";

function provenance(binding: BindingView): string {
  return binding.from_git ? "from git" : "runtime";
}

/** When a binding stops counting, red once that is past: the objects are pushed, the clock is not. */
export function expiry(expiresAt: string | null | undefined): JSX.Element {
  if (!expiresAt) return <Text size="sm">never</Text>;
  const at = new Date(expiresAt);
  return (
    <Text size="sm" c={at.getTime() < Date.now() ? "red" : undefined}>
      {at.toLocaleString()}
    </Text>
  );
}

/** Every rule of every resolved egress policy, one line each: what may be reached and with which credential. */
function EgressPolicySummary({
  egressPolicies,
  missingEgressPolicies,
}: {
  egressPolicies: PolicyView[];
  missingEgressPolicies: string[];
}): JSX.Element {
  return (
    <Stack gap="xs">
      {egressPolicies.map((egressPolicy) => (
        <Stack key={egressPolicy.name} gap={2}>
          <Text size="sm" fw={600}>
            {egressPolicy.name}
          </Text>
          {egressPolicy.rules.map((rule, index) => (
            <Text key={index} size="sm" style={{ overflowWrap: "anywhere" }}>
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
          ))}
        </Stack>
      ))}
      {missingEgressPolicies.map((name) => (
        <Text key={name} size="sm" c="red">
          {name}: no such egress policy
        </Text>
      ))}
    </Stack>
  );
}

/** The bindings whose rules are open, comma-separated; a binding name has no comma in it. */
const RULES_PARAM = "rules";

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

export function EgressBindings({
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
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Binding</Table.Th>
          <Table.Th visibleFrom="sm">Provenance</Table.Th>
          <Table.Th visibleFrom="sm">Expires</Table.Th>
          <Table.Th visibleFrom="sm">Egress policies</Table.Th>
          <Table.Th visibleFrom="sm">Active</Table.Th>
          {onRevoke !== null && <Table.Th />}
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {bindings.length === 0 && (
          <Table.Tr>
            <Table.Td colSpan={onRevoke === null ? 5 : 6}>
              <Text size="sm" c="dimmed">
                No egress binding names this ServiceAccount.
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
                  <Tooltip label="Desired binding, not acknowledgement by every proxy replica">
                    <Badge color="gray">configured</Badge>
                  </Tooltip>
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
              <Table.Td visibleFrom="sm">{expiry(binding.expires_at)}</Table.Td>
              <Table.Td visibleFrom="sm">{policyNames}</Table.Td>
              <Table.Td visibleFrom="sm">
                <Tooltip label="Desired binding, not acknowledgement by every proxy replica">
                  <Badge color="gray">configured</Badge>
                </Tooltip>
              </Table.Td>
              {onRevoke !== null && (
                <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  <BindingActions binding={binding} onRevoke={() => onRevoke(binding.name)} />
                </Table.Td>
              )}
            </Table.Tr>,
          ];
          if (expanded.has(binding.name)) {
            rows.push(
              <Table.Tr key={`${binding.name}-rules`}>
                <Table.Td colSpan={onRevoke === null ? 5 : 6}>
                  <EgressPolicySummary
                    egressPolicies={binding.policies}
                    missingEgressPolicies={binding.missing_policies}
                  />
                </Table.Td>
              </Table.Tr>
            );
          }
          return rows;
        })}
      </Table.Tbody>
      <Table.Caption>
        An unexpired egress binding grants its configured rules; revoking deletes it. One from the repository is removed
        there.
      </Table.Caption>
    </Table>
  );
}

/** Grants to a sandbox that is already running; each grant is its own binding, revoked on its own. */
function GrantEgressPolicies({
  egressPolicies,
  picked,
  onPick,
  onGrant,
}: {
  egressPolicies: string[];
  picked: string[];
  onPick: (names: string[]) => void;
  onGrant: () => void;
}): JSX.Element {
  return (
    <Group align="flex-end">
      <MultiSelect
        label="Grant egress policies"
        description="Added as a binding of its own; what this sandbox already has is untouched"
        hidePickedOptions
        data={egressPolicies}
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

/** The groups whose requests are listed, comma-separated; a group key has no comma in it. */
const GROUPS_PARAM = "decision_groups";
/** Present while the log is shown as one row per request. */
const EVERY_REQUEST_PARAM = "every_request";
/** Requests listed under an open group before "Show all". */
const MEMBERS_SHOWN = 50;

function reasonOf(decision: Decision): string {
  return decision.outcome === "allow"
    ? [decision.policy, decision.substituted ? "credential substituted" : null].filter((part) => part).join(" · ")
    : (decision.reason ?? "");
}

function destination(decision: { host: string; port: number }): string {
  return `${decision.host}${decision.port !== 443 ? `:${decision.port}` : ""}`;
}

function DecisionRow({ decision, nested }: { decision: Decision; nested: boolean }): JSX.Element {
  const reason = reasonOf(decision);
  return (
    <Table.Tr>
      <Table.Td pl={nested ? "xl" : undefined}>
        <Text size="sm" style={{ whiteSpace: "nowrap" }}>
          {new Date(decision.at).toLocaleTimeString()}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="sm">{decision.method}</Text>
      </Table.Td>
      <Table.Td>
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {destination(decision)}
        </Text>
        {decision.address && (
          <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
            {decision.address}
          </Text>
        )}
        <Text size="xs" c="dimmed" hiddenFrom="sm" style={{ overflowWrap: "anywhere" }}>
          {reason}
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
}

function DecisionGroupRows({
  group,
  open,
  onToggle,
}: {
  group: DecisionGroup;
  open: boolean;
  onToggle: () => void;
}): JSX.Element {
  const [showAll, setShowAll] = useState(false);
  if (group.members.length === 1) return <DecisionRow decision={group.members[0]} nested={false} />;
  const reason = reasonOf({ ...group.members[0], substituted: group.substituted });
  const listed = showAll ? group.members : group.members.slice(0, MEMBERS_SHOWN);
  return (
    <>
      <Table.Tr>
        <Table.Td>
          <Tooltip label={`First ${new Date(group.firstAt).toLocaleTimeString()}`} withArrow>
            <Text size="sm" style={{ whiteSpace: "nowrap" }}>
              {new Date(group.lastAt).toLocaleTimeString()}
            </Text>
          </Tooltip>
        </Table.Td>
        <Table.Td>
          <Text size="sm">{group.method}</Text>
        </Table.Td>
        <Table.Td>
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            {destination(group)}
          </Text>
          <Button size="compact-xs" variant="subtle" onClick={onToggle} aria-expanded={open}>
            {open ? "Hide" : "Show"} {group.members.length} requests
          </Button>
          <Text size="xs" c="dimmed" hiddenFrom="sm" style={{ overflowWrap: "anywhere" }}>
            {reason}
          </Text>
        </Table.Td>
        <Table.Td>
          <Badge color={group.outcome === "allow" ? "green" : "red"}>{group.outcome}</Badge>
        </Table.Td>
        <Table.Td visibleFrom="sm">
          <Text size="sm">{reason || "—"}</Text>
        </Table.Td>
      </Table.Tr>
      {open && listed.map((member, index) => <DecisionRow key={index} decision={member} nested />)}
      {open && listed.length < group.members.length && (
        <Table.Tr>
          <Table.Td colSpan={5} pl="xl">
            <Button size="compact-xs" variant="subtle" onClick={() => setShowAll(true)}>
              Show all {group.members.length}
            </Button>
          </Table.Td>
        </Table.Tr>
      )}
    </>
  );
}

function DecisionsTable({ decisions }: { decisions: Decision[] }): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const everyRequest = searchParams.has(EVERY_REQUEST_PARAM);
  const open = new Set((searchParams.get(GROUPS_PARAM) ?? "").split(",").filter((key) => key));
  function update(change: (params: URLSearchParams) => void): void {
    const params = new URLSearchParams(searchParams);
    change(params);
    setSearchParams(params, { replace: true });
  }
  function toggle(key: string): void {
    const next = new Set(open);
    if (!next.delete(key)) next.add(key);
    update((params) => (next.size === 0 ? params.delete(GROUPS_PARAM) : params.set(GROUPS_PARAM, [...next].join(","))));
  }
  const groups = groupDecisions(decisions);
  return (
    <Stack gap="xs">
      <SegmentedControl
        size="xs"
        value={everyRequest ? "every" : "grouped"}
        onChange={(value) =>
          update((params) =>
            value === "every" ? params.set(EVERY_REQUEST_PARAM, "") : params.delete(EVERY_REQUEST_PARAM)
          )
        }
        data={[
          { value: "grouped", label: "Grouped" },
          { value: "every", label: "Every request" },
        ]}
      />
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>{everyRequest ? "Time" : "Last"}</Table.Th>
            <Table.Th>Method</Table.Th>
            <Table.Th>Host</Table.Th>
            <Table.Th>Decision</Table.Th>
            <Table.Th visibleFrom="sm">Reason</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {decisions.length === 0 && (
            <Table.Tr>
              <Table.Td colSpan={5}>
                <Text size="sm" c="dimmed">
                  No decisions yet.
                </Text>
              </Table.Td>
            </Table.Tr>
          )}
          {everyRequest
            ? // Newest first: the log is served oldest first.
              [...decisions]
                .reverse()
                .map((decision, index) => <DecisionRow key={index} decision={decision} nested={false} />)
            : groups.map((group) => (
                <DecisionGroupRows
                  key={group.key}
                  group={group}
                  open={open.has(group.key)}
                  onToggle={() => toggle(group.key)}
                />
              ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

/** The sandbox's decisions as the proxy reports them: a snapshot per change, none while it is quiet. */
function useEgressDecisions(name: string): { snapshot: DecisionsSnapshot | null; stream: StreamStatus } {
  const [snapshot, setSnapshot] = useState<DecisionsSnapshot | null>(null);
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  useEffect(() => {
    setSnapshot(null);
    return followStream(liveEgressDecisionsUrl(name), {
      events: {
        snapshot: (message) => setSnapshot(JSON.parse(message.data) as DecisionsSnapshot),
        health: (message) =>
          setSnapshot((current) => current && { ...current, ...(JSON.parse(message.data) as { watching: boolean }) }),
      },
      onConnection: setConnection,
    });
  }, [name]);
  return { snapshot, stream: useStreamStatus(`Egress decisions of ${name}`, connection) };
}

/** What may leave the sandbox and what recently did: its pushed bindings and the proxy's decisions. */
export function EgressSection({ name, bindings }: { name: string; bindings: BindingView[] | null }): JSX.Element {
  const decisions = useEgressDecisions(name);
  const [error, setError] = useState<string | null>(null);
  // The namespace's egress policies, and the ones picked to grant this sandbox next.
  const [egressPolicies, setEgressPolicies] = useState<string[]>([]);
  const [picked, setPicked] = useState<string[]>([]);

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await api.GET("/egress/policies");
      setError(failure ? displayableError(failure) : null);
      if (!failure) setEgressPolicies(data.map((policy) => policy.name));
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
      <GrantEgressPolicies
        egressPolicies={egressPolicies}
        picked={picked}
        onPick={setPicked}
        onGrant={() => void grant()}
      />
      {bindings && <EgressBindings bindings={bindings} onRevoke={(binding) => void revoke(binding)} />}
      <Title order={5}>Recent decisions</Title>
      <StaleNotice streams={[decisions.stream]} />
      {decisions.snapshot !== null && !decisions.snapshot.watching && (
        <Text size="sm" c="dimmed">
          Not hearing from the egress proxy about new decisions; this log may be behind.
        </Text>
      )}
      {decisions.snapshot !== null &&
        (Array.isArray(decisions.snapshot.decisions) ? (
          <DecisionsTable decisions={decisions.snapshot.decisions} />
        ) : (
          <Text size="sm" c="dimmed">
            The egress proxy could not be asked; the rules above still apply. ({decisions.snapshot.decisions.detail})
          </Text>
        ))}
    </Stack>
  );
}
