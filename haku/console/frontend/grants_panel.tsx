import { Badge, Button, Code, Group, Loader, SegmentedControl, Select, Stack, Table, Text } from "@mantine/core";
import { useCallback, useEffect, useMemo, useState } from "react";

import { displayableError, fetchGrants, revokeGrant, type Grant, type GrantPrincipal } from "./client";
import { useAgentNames } from "./agent_names";
import { CodeBlock } from "./code_block";
import { GrantPrincipalLabel } from "./grant_principal";
import { ExternalLink } from "./link";
import { toolCallPath } from "./routing";
import { formatTimestamp } from "./time";
import { toastError, toastSuccess } from "./toast";

export type GrantHistoryFilter = "active" | "history" | "all";

type KubernetesRulesCoverage = Extract<Grant["coverage"], { kind: "kubernetes_rules" }>;
type KubernetesGrantScope = KubernetesRulesCoverage["scope"];
type KubernetesRule = KubernetesRulesCoverage["rules"][number];
const STATUS_DISPLAY: Record<Grant["validity"]["status"], { label: string; color: string }> = {
  active: { label: "Active", color: "teal" },
  expired: { label: "Expired", color: "gray" },
  ended: { label: "Ended", color: "gray" },
};

function scopeLabel(scope: KubernetesGrantScope): string {
  switch (scope.kind) {
    case "namespaces":
      return `Namespaces: ${scope.namespaces?.join(", ") ?? ""}`;
    case "all_namespaces":
      return "All namespaced resources";
    case "cluster":
      return "Cluster-scoped resources";
    case "non_resource":
      return "Kubernetes non-resource URLs";
  }
}

function RuleLine({ rule }: { rule: KubernetesRule }) {
  const verbs = rule.verbs.join(", ");
  const nonResourceUrls = rule.non_resource_urls ?? [];
  if (nonResourceUrls.length > 0) {
    return (
      <Text size="xs">
        <Code>{verbs}</Code> {nonResourceUrls.join(", ")}
      </Text>
    );
  }
  const groups = (rule.api_groups ?? []).map((group) => group || "core").join(", ");
  const resourceNames = rule.resource_names ?? [];
  const names = resourceNames.length > 0 ? ` · names ${resourceNames.join(", ")}` : "";
  return (
    <Text size="xs">
      <Code>{verbs}</Code> {(rule.resources ?? []).join(", ")}{" "}
      <Text span c="dimmed">
        · API {groups}
        {names}
      </Text>
    </Text>
  );
}

function GrantFallback({ grant }: { grant: Grant }) {
  return <CodeBlock language="json" value={JSON.stringify(grant, null, 2)} />;
}

function GrantSource({ grant }: { grant: Grant }) {
  switch (grant.source.kind) {
    case "database": {
      return (
        <Stack gap={2}>
          <Text size="sm">Database</Text>
          <ExternalLink
            href={toolCallPath(grant.source.tool_call_id)}
            size="xs"
            title={`Open tool call ${grant.source.tool_call_id}`}
            aria-label={`Open tool call ${grant.source.tool_call_id}`}
          >
            tool call
          </ExternalLink>
        </Stack>
      );
    }
    case "config_file":
      return (
        <Stack gap={2}>
          <Text size="sm">Configuration</Text>
          <Text size="xs" c="dimmed" ff="monospace">
            {grant.source.entry_id}
          </Text>
        </Stack>
      );
  }
  return <GrantFallback grant={grant} />;
}

function GrantSubject({ grant }: { grant: Grant }) {
  return (
    <Text size="sm">
      <GrantPrincipalLabel principal={grant.subject} />
    </Text>
  );
}

function principalKey(principal: GrantPrincipal): string {
  return JSON.stringify(principal);
}

function principalLabel(principal: GrantPrincipal, agentNames: ReadonlyMap<string, string>): string {
  switch (principal.kind) {
    case "agent":
      return `Agent ${agentNames.get(principal.agent_id) ?? "Unknown agent"}`;
    case "session":
      return `Session ${principal.session_id}`;
    case "access_profile":
      return `Access profile ${principal.access_profile_id}`;
  }
}

function GrantCoverage({ grant }: { grant: Grant }) {
  switch (grant.coverage.kind) {
    case "kubernetes_rules":
      return (
        <Stack gap={2}>
          <Text size="sm" fw={600}>
            {scopeLabel(grant.coverage.scope)}
          </Text>
          <Stack gap={2}>
            {grant.coverage.rules.map((rule, index) => (
              <RuleLine key={index} rule={rule} />
            ))}
          </Stack>
        </Stack>
      );
    case "kubernetes_sar":
      return <Text size="sm">SubjectAccessReview · {grant.coverage.subject.username}</Text>;
    case "http":
      return (
        <Stack gap={2}>
          <Text size="sm" fw={600}>
            {grant.coverage.origins.map((origin) => `${origin.scheme}://${origin.host}:${origin.port}`).join(", ")}
          </Text>
          <Text size="xs">
            {grant.coverage.coverage.methods.join(", ")} {grant.coverage.coverage.path_regex ?? "all paths"}
          </Text>
        </Stack>
      );
  }
  return <GrantFallback grant={grant} />;
}

function GrantValidity({ grant }: { grant: Grant }) {
  const status = STATUS_DISPLAY[grant.validity.status];
  const endsAt = grant.validity.ends_at ? formatTimestamp(grant.validity.ends_at) : null;
  const endedAt = grant.validity.ended_at ? formatTimestamp(grant.validity.ended_at) : null;
  return (
    <Stack gap={2}>
      <Badge size="sm" color={status.color} variant="light" style={{ alignSelf: "flex-start" }}>
        {status.label}
      </Badge>
      {endsAt && (
        <Text size="xs" c="dimmed" title={endsAt.title}>
          expires {endsAt.text}
        </Text>
      )}
      {endedAt && (
        <Text size="xs" c="dimmed" title={endedAt.title}>
          ended {endedAt.text}
          {grant.validity.end_reason ? ` · ${grant.validity.end_reason}` : ""}
        </Text>
      )}
    </Stack>
  );
}

function GrantRow({
  grant,
  revokePending,
  revokeBusy,
  onRequestRevoke,
  onCancelRevoke,
  onConfirmRevoke,
}: {
  grant: Grant;
  revokePending: boolean;
  revokeBusy: boolean;
  onRequestRevoke: (grantId: string) => void;
  onCancelRevoke: () => void;
  onConfirmRevoke: (grantId: string) => void;
}) {
  const databaseSource = grant.source.kind === "database" ? grant.source : null;
  return (
    <Table.Tr>
      <Table.Td data-slot="primary" className="haku-dense-primary">
        <GrantSubject grant={grant} />
      </Table.Td>
      <Table.Td data-slot="secondary" className="haku-dense-secondary">
        <GrantCoverage grant={grant} />
      </Table.Td>
      <Table.Td data-slot="extra" className="haku-dense-secondary">
        <GrantSource grant={grant} />
      </Table.Td>
      <Table.Td data-slot="status" className="haku-dense-status">
        <GrantValidity grant={grant} />
      </Table.Td>
      <Table.Td data-slot="action" className="haku-dense-action">
        {databaseSource !== null &&
          grant.validity.status === "active" &&
          (revokePending ? (
            <Group gap="xs" wrap="nowrap">
              <Button
                size="compact-sm"
                color="red"
                onClick={() => onConfirmRevoke(databaseSource.id)}
                loading={revokeBusy}
              >
                Confirm
              </Button>
              <Button size="compact-sm" color="gray" variant="subtle" onClick={onCancelRevoke} disabled={revokeBusy}>
                Cancel
              </Button>
            </Group>
          ) : (
            <Button size="compact-sm" color="red" variant="light" onClick={() => onRequestRevoke(databaseSource.id)}>
              Revoke
            </Button>
          ))}
      </Table.Td>
    </Table.Tr>
  );
}

export function GrantsPanel(): JSX.Element {
  const agentNames = useAgentNames();
  const [allGrants, setAllGrants] = useState<Grant[] | null>(null);
  const [grants, setGrants] = useState<Grant[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [historyFilter, setHistoryFilter] = useState<GrantHistoryFilter>("active");
  const [selectedPrincipal, setSelectedPrincipal] = useState<string | null>(null);
  const [revokingGrantId, setRevokingGrantId] = useState<string | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);

  const load = useCallback((principal?: GrantPrincipal) => {
    setLoading(true);
    void fetchGrants(principal).then(
      (grantResponse) => {
        setGrants(grantResponse.grants);
        if (principal === undefined) setAllGrants(grantResponse.grants);
        setError(null);
        setLoading(false);
      },
      (e: unknown) => {
        setError(displayableError(e));
        setLoading(false);
      }
    );
  }, []);

  // This panel is mounted only while its Settings tab is active.
  useEffect(load, [load]);

  const principals = useMemo(() => {
    const byKey = new Map((allGrants ?? []).map((grant) => [principalKey(grant.subject), grant.subject]));
    return [...byKey.entries()]
      .map(([value, principal]) => ({ value, label: principalLabel(principal, agentNames) }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [agentNames, allGrants]);

  const principalsByKey = useMemo(
    () => new Map((allGrants ?? []).map((grant) => [principalKey(grant.subject), grant.subject])),
    [allGrants]
  );

  const visible = useMemo(
    () =>
      (grants ?? []).filter(
        (item) =>
          historyFilter === "all" ||
          (historyFilter === "active" ? item.validity.status === "active" : item.validity.status !== "active")
      ),
    [grants, historyFilter]
  );

  function confirmRevoke(grantId: string) {
    setRevokeBusy(true);
    void revokeGrant(grantId).then(
      (response) => {
        const updates = new Map(
          response.grants.flatMap((grant) =>
            grant.source.kind === "database" ? [[grant.source.id, grant.validity] as const] : []
          )
        );
        setGrants(
          (current) =>
            current?.map((grant) =>
              grant.source.kind === "database" && updates.has(grant.source.id)
                ? { ...grant, validity: updates.get(grant.source.id)! }
                : grant
            ) ?? null
        );
        setAllGrants(
          (current) =>
            current?.map((grant) =>
              grant.source.kind === "database" && updates.has(grant.source.id)
                ? { ...grant, validity: updates.get(grant.source.id)! }
                : grant
            ) ?? null
        );
        setRevokeBusy(false);
        setRevokingGrantId(null);
        toastSuccess("Grant revoked", "The active grant has ended.");
      },
      (e: unknown) => {
        setRevokeBusy(false);
        toastError("Couldn't revoke grant", e);
      }
    );
  }

  return (
    <Stack gap="xs" className="haku-page-list">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="wrap">
        <div>
          <Text fw={600}>Grants</Text>
        </div>
        <Button
          size="xs"
          variant="light"
          color="gray"
          loading={loading}
          onClick={() => load(selectedPrincipal === null ? undefined : principalsByKey.get(selectedPrincipal))}
        >
          Refresh
        </Button>
      </Group>
      <Group gap="sm" align="flex-end" wrap="wrap">
        <Stack gap={4}>
          <Text size="xs" fw={500}>
            Lifecycle
          </Text>
          <SegmentedControl
            size="xs"
            value={historyFilter}
            onChange={(value) => setHistoryFilter(value as GrantHistoryFilter)}
            data={[
              { value: "active", label: "Active" },
              { value: "history", label: "History" },
              { value: "all", label: "All" },
            ]}
          />
        </Stack>
        <Select
          size="xs"
          label="Principal"
          placeholder="All principals"
          data={principals}
          value={selectedPrincipal}
          onChange={(value) => {
            setSelectedPrincipal(value);
            if (value === null) {
              setGrants(allGrants);
              return;
            }
            const principal = principalsByKey.get(value);
            if (principal) load(principal);
          }}
          clearable
          style={{ minWidth: 190 }}
        />
      </Group>
      {error && (
        <Text c="red" size="sm">
          Failed to load grants: {error}
        </Text>
      )}
      {!grants && !error && (
        <Group justify="center" p="xl">
          <Loader aria-label="Loading grants" />
        </Group>
      )}
      {grants && visible.length === 0 && (
        <div className="haku-empty-state">
          <Text size="sm" c="dimmed">
            No {historyFilter === "all" ? "" : `${historyFilter} `}grants match these filters.
          </Text>
        </div>
      )}
      {visible.length > 0 && (
        <Table.ScrollContainer minWidth={0} className="haku-dense-table-wrap">
          <Table className="haku-dense-table" aria-label="Grants" highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Principal</Table.Th>
                <Table.Th>Scope / rules</Table.Th>
                <Table.Th>Source</Table.Th>
                <Table.Th>Validity</Table.Th>
                <Table.Th aria-label="Actions" />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {visible.map((grant) => {
                const sourceId = grant.source.kind === "database" ? grant.source.id : grant.source.entry_id;
                return (
                  <GrantRow
                    key={`${principalKey(grant.subject)}:${sourceId}`}
                    grant={grant}
                    revokePending={grant.source.kind === "database" && revokingGrantId === grant.source.id}
                    revokeBusy={revokeBusy}
                    onRequestRevoke={setRevokingGrantId}
                    onCancelRevoke={() => setRevokingGrantId(null)}
                    onConfirmRevoke={confirmRevoke}
                  />
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </Stack>
  );
}
