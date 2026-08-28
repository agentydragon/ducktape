import {
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  Text,
  Textarea,
} from "@mantine/core";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  displayableError,
  fetchKubernetesGrants,
  revokeKubernetesGrantSet,
  type OperatorKubernetesGrant,
} from "./client";
import { formatTimestamp } from "./approval_state";
import { ExternalLink } from "./link";
import { toolCallPath } from "./routing";
import { toastError, toastSuccess } from "./toast";
import { principalText } from "./tool_rendering/kubernetes/responses";

export type GrantHistoryFilter = "active" | "history" | "all";

type KubernetesGrantSet = {
  agentId: string;
  agentDisplayName: string;
  sourceToolCallId: string;
  grants: OperatorKubernetesGrant[];
};

const STATUS_DISPLAY: Record<OperatorKubernetesGrant["grant"]["status"], { label: string; color: string }> = {
  active: { label: "Active", color: "teal" },
  expired: { label: "Expired", color: "gray" },
  released: { label: "Released by Agent", color: "blue" },
  revoked: { label: "Revoked by Operator", color: "red" },
};

function scopeLabel(scope: OperatorKubernetesGrant["grant"]["scope"]): string {
  switch (scope.kind) {
    case "namespaces":
      return `Namespaces: ${scope.namespaces.join(", ")}`;
    case "all_namespaces":
      return "All namespaced resources";
    case "cluster":
      return "Cluster-scoped resources";
    case "non_resource":
      return "Kubernetes non-resource URLs";
  }
}

function RuleLine({ rule }: { rule: OperatorKubernetesGrant["grant"]["rules"][number] }) {
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

function GrantSetCard({ item, onRevoke }: { item: KubernetesGrantSet; onRevoke: (item: KubernetesGrantSet) => void }) {
  const first = item.grants[0];
  if (!first) return null;
  const activeCount = item.grants.filter(({ grant }) => grant.status === "active").length;
  const created = formatTimestamp(first.grant.created_at);
  const expires = formatTimestamp(first.grant.expires_at);
  return (
    <section className="haku-shell-card">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600}>{item.agentDisplayName}</Text>
          <Text size="xs" c="dimmed" ff="monospace">
            {item.grants.length} grant{item.grants.length === 1 ? "" : "s"} from one approval
          </Text>
          <Text size="xs" c="dimmed">
            Applies to {principalText(first.grant.principal)}
          </Text>
        </Stack>
        <Badge color={activeCount > 0 ? "teal" : "gray"} variant="light" style={{ flexShrink: 0 }}>
          {activeCount > 0 ? `${activeCount} active` : "Ended"}
        </Badge>
      </Group>

      <Stack gap="xs" mt="sm">
        {item.grants.map(({ grant }) => {
          const status = STATUS_DISPLAY[grant.status];
          const endedAt = grant.released_at ?? grant.revoked_at;
          const ended = endedAt ? formatTimestamp(endedAt) : null;
          return (
            <Stack
              key={grant.grant_id}
              gap={4}
              p="xs"
              style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 6 }}
            >
              <Group justify="space-between" gap="sm" wrap="nowrap">
                <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: "anywhere" }}>
                  {grant.grant_id}
                </Text>
                <Badge size="xs" color={status.color} variant="light" style={{ flexShrink: 0 }}>
                  {status.label}
                </Badge>
              </Group>
              <Text size="sm">{scopeLabel(grant.scope)}</Text>
              <Stack gap={4}>
                {grant.rules.map((rule, index) => (
                  <RuleLine key={index} rule={rule} />
                ))}
              </Stack>
              {ended && (
                <Text size="xs" c="dimmed" title={ended.title}>
                  Ended {ended.text}
                  {grant.end_reason ? ` · ${grant.end_reason}` : ""}
                </Text>
              )}
            </Stack>
          );
        })}
        <Group gap="md" wrap="wrap">
          <Text size="xs" title={created.title}>
            <Text span c="dimmed">
              Created{" "}
            </Text>
            {created.text}
          </Text>
          <Text size="xs" title={expires.title}>
            <Text span c="dimmed">
              Expires{" "}
            </Text>
            {expires.text}
          </Text>
        </Group>
        <Group justify="space-between" align="center" gap="sm" wrap="wrap">
          <ExternalLink href={toolCallPath(item.sourceToolCallId)} size="xs" ff="monospace">
            Source tool call {item.sourceToolCallId}
          </ExternalLink>
          {activeCount > 0 && (
            <Button size="compact-sm" color="red" variant="light" onClick={() => onRevoke(item)}>
              Revoke active set…
            </Button>
          )}
        </Group>
      </Stack>
    </section>
  );
}

function RevokeDialog({
  item,
  busy,
  onClose,
  onConfirm,
}: {
  item: KubernetesGrantSet | null;
  busy: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  useEffect(() => setReason(""), [item?.sourceToolCallId]);
  const activeCount = item?.grants.filter(({ grant }) => grant.status === "active").length ?? 0;
  return (
    <Modal
      opened={item !== null}
      onClose={busy ? () => undefined : onClose}
      title="Revoke Kubernetes grant set"
      centered
      returnFocus
    >
      <Stack gap="sm">
        <Text size="sm">
          End all {activeCount} active grants created by this approval for <strong>{item?.agentDisplayName}</strong>
          immediately.
        </Text>
        {item && (
          <Text size="xs" c="dimmed" ff="monospace">
            {item.sourceToolCallId}
          </Text>
        )}
        <Textarea
          label="Revocation reason"
          description="Required and retained with the grant's audit history."
          placeholder="Why is this grant being revoked?"
          value={reason}
          onChange={(event) => setReason(event.currentTarget.value)}
          minRows={3}
          maxLength={500}
          required
          disabled={busy}
          autoFocus
        />
        <Group justify="flex-end">
          <Button variant="subtle" color="gray" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button color="red" onClick={() => onConfirm(reason.trim())} disabled={!reason.trim()} loading={busy}>
            Revoke grant set
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function KubernetesGrantsPanel(): JSX.Element {
  const [grants, setGrants] = useState<OperatorKubernetesGrant[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [historyFilter, setHistoryFilter] = useState<GrantHistoryFilter>("active");
  const [agentId, setAgentId] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<KubernetesGrantSet | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    void fetchKubernetesGrants().then(
      (response) => {
        setGrants(response.grants);
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

  const agents = useMemo(() => {
    const names = new Map<string, string>();
    for (const item of grants ?? []) names.set(item.grant.owner_agent_id, item.agent_display_name);
    return [...names].map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label));
  }, [grants]);

  const grantSets = useMemo(() => {
    const sets = new Map<string, KubernetesGrantSet>();
    for (const item of grants ?? []) {
      const key = `${item.grant.owner_agent_id}:${item.grant.source_tool_call_id}`;
      const current = sets.get(key);
      if (current) {
        current.grants.push(item);
      } else {
        sets.set(key, {
          agentId: item.grant.owner_agent_id,
          agentDisplayName: item.agent_display_name,
          sourceToolCallId: item.grant.source_tool_call_id,
          grants: [item],
        });
      }
    }
    return [...sets.values()].map((set) => ({
      ...set,
      grants: set.grants.sort((left, right) => left.grant.grant_id.localeCompare(right.grant.grant_id)),
    }));
  }, [grants]);

  const visible = useMemo(
    () =>
      grantSets.filter((item) => {
        if (agentId !== null && item.agentId !== agentId) return false;
        const active = item.grants.some(({ grant }) => grant.status === "active");
        if (historyFilter === "active") return active;
        if (historyFilter === "history") return !active;
        return true;
      }),
    [agentId, grantSets, historyFilter]
  );

  function confirmRevoke(reason: string) {
    if (!revoking || !reason) return;
    const target = revoking;
    setRevokeBusy(true);
    void revokeKubernetesGrantSet(target.agentId, target.sourceToolCallId, reason).then(
      (response) => {
        const updates = new Map(response.grants.map((item) => [item.grant.grant_id, item]));
        setGrants((current) => current?.map((item) => updates.get(item.grant.grant_id) ?? item) ?? null);
        setRevokeBusy(false);
        setRevoking(null);
        toastSuccess(
          "Kubernetes grant set revoked",
          `${target.agentDisplayName} no longer has the active grants from this approval.`
        );
      },
      (e: unknown) => {
        setRevokeBusy(false);
        toastError("Couldn't revoke Kubernetes grant set", e);
      }
    );
  }

  return (
    <Stack gap="xs" className="haku-page-list">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="wrap">
        <div>
          <Text fw={600}>Kubernetes grants</Text>
          <Text size="xs" c="dimmed" mt={4}>
            Time-bounded capabilities approved for your Agents, with exact scope, rules, provenance, and lifecycle
            history.
          </Text>
        </div>
        <Button size="xs" variant="light" color="gray" loading={loading} onClick={load}>
          Refresh
        </Button>
      </Group>
      <Alert color="blue" variant="light" title="Temporary grants">
        These are operator-approved, time-bounded additions. Standing SubjectAccessReview access is separate and never
        appears here. Denied requests do not create grants.
      </Alert>
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
          label="Agent"
          placeholder="All Agents"
          data={agents}
          value={agentId}
          onChange={setAgentId}
          clearable
          style={{ minWidth: 190 }}
        />
      </Group>
      {error && (
        <Text c="red" size="sm">
          Failed to load Kubernetes grants: {error}
        </Text>
      )}
      {!grants && !error && (
        <Group justify="center" p="xl">
          <Loader aria-label="Loading Kubernetes grants" />
        </Group>
      )}
      {grants && visible.length === 0 && (
        <section className="haku-shell-card">
          <Text size="sm" c="dimmed">
            No {historyFilter === "all" ? "" : `${historyFilter} `}Kubernetes grants match these filters.
          </Text>
        </section>
      )}
      {visible.map((item) => (
        <GrantSetCard key={`${item.agentId}:${item.sourceToolCallId}`} item={item} onRevoke={setRevoking} />
      ))}
      <RevokeDialog item={revoking} busy={revokeBusy} onClose={() => setRevoking(null)} onConfirm={confirmRevoke} />
    </Stack>
  );
}
