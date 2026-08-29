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

import { displayableError, fetchKubernetesGrants, revokeGrant, type OperatorKubernetesGrant } from "./client";
import { formatTimestamp } from "./approval_state";
import { CodeBlock } from "./code_block";
import { ExternalLink } from "./link";
import { toolCallPath } from "./routing";
import { toastError, toastSuccess } from "./toast";
import { principalText } from "./tool_rendering/grants/responses";

export type GrantHistoryFilter = "active" | "history" | "all";

type KubernetesGrant = OperatorKubernetesGrant["grant"];
type KubernetesRulesCoverage = Extract<KubernetesGrant["coverage"], { kind: "kubernetes_rules" }>;
type KubernetesGrantScope = KubernetesRulesCoverage["scope"];
type KubernetesRule = KubernetesRulesCoverage["rules"][number];
type RevokeTarget = {
  agentId: string;
  agentDisplayName: string;
  grantId: string;
};

const STATUS_DISPLAY: Record<KubernetesGrant["validity"]["status"], { label: string; color: string }> = {
  active: { label: "Active", color: "teal" },
  expired: { label: "Expired", color: "gray" },
  released: { label: "Released by Agent", color: "blue" },
  revoked: { label: "Revoked by Operator", color: "red" },
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

function GrantFallback({ grant }: { grant: KubernetesGrant }) {
  return <CodeBlock language="json" value={JSON.stringify(grant, null, 2)} />;
}

function GrantSource({ grant }: { grant: KubernetesGrant }) {
  switch (grant.source.kind) {
    case "database": {
      const created = formatTimestamp(grant.source.created_at);
      return (
        <>
          <Badge color="violet" variant="light">
            Database
          </Badge>
          <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: "anywhere" }}>
            {grant.source.id}
          </Text>
          <Text size="xs" c="dimmed" title={created.title}>
            Created {created.text}
          </Text>
          <ExternalLink href={toolCallPath(grant.source.tool_call_id)} size="xs" ff="monospace">
            Source tool call {grant.source.tool_call_id}
          </ExternalLink>
        </>
      );
    }
    case "config_file":
      return (
        <>
          <Badge color="blue" variant="light">
            Configuration file
          </Badge>
          <Text size="xs" c="dimmed" ff="monospace">
            {grant.source.entry_id}
          </Text>
        </>
      );
  }
  return <GrantFallback grant={grant} />;
}

function GrantSubject({ grant }: { grant: KubernetesGrant }) {
  switch (grant.subject.kind) {
    case "grant_principal":
      return <Text size="xs">Applies to {principalText(grant.subject.principal)}</Text>;
    case "access_profile":
      return <Text size="xs">Access profile {grant.subject.access_profile_id}</Text>;
  }
  return <GrantFallback grant={grant} />;
}

function GrantCoverage({ grant }: { grant: KubernetesGrant }) {
  switch (grant.coverage.kind) {
    case "kubernetes_rules":
      return (
        <>
          <Text size="sm">{scopeLabel(grant.coverage.scope)}</Text>
          <Stack gap={4}>
            {grant.coverage.rules.map((rule, index) => (
              <RuleLine key={index} rule={rule} />
            ))}
          </Stack>
        </>
      );
    case "kubernetes_sar":
      return <Text size="sm">Kubernetes SubjectAccessReview identity: {grant.coverage.subject.username}</Text>;
  }
  return <GrantFallback grant={grant} />;
}

function GrantValidity({ grant }: { grant: KubernetesGrant }) {
  const status = STATUS_DISPLAY[grant.validity.status];
  const endsAt = grant.validity.ends_at ? formatTimestamp(grant.validity.ends_at) : null;
  const endedAt = grant.validity.ended_at ? formatTimestamp(grant.validity.ended_at) : null;
  return (
    <Stack gap={2}>
      <Badge size="xs" color={status.color} variant="light" style={{ alignSelf: "flex-start" }}>
        {status.label}
      </Badge>
      {endsAt && (
        <Text size="xs" c="dimmed" title={endsAt.title}>
          Expires {endsAt.text}
        </Text>
      )}
      {endedAt && (
        <Text size="xs" c="dimmed" title={endedAt.title}>
          Ended {endedAt.text}
          {grant.validity.end_reason ? ` · ${grant.validity.end_reason}` : ""}
        </Text>
      )}
    </Stack>
  );
}

function GrantCard({ item, onRevoke }: { item: OperatorKubernetesGrant; onRevoke: (target: RevokeTarget) => void }) {
  const { grant } = item;
  return (
    <section className="haku-shell-card">
      <Stack gap="xs">
        <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
          <Text fw={600}>{item.agent_display_name}</Text>
          <GrantValidity grant={grant} />
        </Group>
        <GrantSource grant={grant} />
        <GrantSubject grant={grant} />
        <GrantCoverage grant={grant} />
        {grant.source.kind === "database" && grant.validity.status === "active" && (
          <Button
            size="compact-sm"
            color="red"
            variant="light"
            onClick={() =>
              onRevoke({
                agentId: item.agent_id,
                agentDisplayName: item.agent_display_name,
                grantId: grant.source.id,
              })
            }
          >
            Revoke grant…
          </Button>
        )}
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
  item: RevokeTarget | null;
  busy: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  useEffect(() => setReason(""), [item?.grantId]);
  return (
    <Modal
      opened={item !== null}
      onClose={busy ? () => undefined : onClose}
      title="Revoke Kubernetes grant"
      centered
      returnFocus
    >
      <Stack gap="sm">
        <Text size="sm">
          End this active grant for <strong>{item?.agentDisplayName}</strong> immediately.
        </Text>
        {item && (
          <Text size="xs" c="dimmed" ff="monospace">
            {item.grantId}
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
            Revoke grant
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
  const [revoking, setRevoking] = useState<RevokeTarget | null>(null);
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
    for (const item of grants ?? []) names.set(item.agent_id, item.agent_display_name);
    return [...names].map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label));
  }, [grants]);

  const visible = useMemo(
    () =>
      (grants ?? []).filter(
        (item) =>
          (agentId === null || item.agent_id === agentId) &&
          (historyFilter === "all" ||
            (historyFilter === "active"
              ? item.grant.validity.status === "active"
              : item.grant.validity.status !== "active"))
      ),
    [agentId, grants, historyFilter]
  );

  function confirmRevoke(reason: string) {
    if (!revoking || !reason) return;
    const target = revoking;
    setRevokeBusy(true);
    void revokeGrant(target.agentId, target.grantId, reason).then(
      (response) => {
        const updates = new Map(
          response.grants.flatMap((item) =>
            item.grant.source.kind === "database" ? [[item.grant.source.id, item.grant.validity] as const] : []
          )
        );
        setGrants(
          (current) =>
            current?.map((item) =>
              item.grant.source.kind === "database" && updates.has(item.grant.source.id)
                ? { ...item, grant: { ...item.grant, validity: updates.get(item.grant.source.id)! } }
                : item
            ) ?? null
        );
        setRevokeBusy(false);
        setRevoking(null);
        toastSuccess("Kubernetes grant revoked", `${target.agentDisplayName} no longer has this active grant.`);
      },
      (e: unknown) => {
        setRevokeBusy(false);
        toastError("Couldn't revoke Kubernetes grant", e);
      }
    );
  }

  return (
    <Stack gap="xs" className="haku-page-list">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="wrap">
        <div>
          <Text fw={600}>Kubernetes grants</Text>
          <Text size="xs" c="dimmed" mt={4}>
            Configuration-file and database authority for your Agents, with exact scope, rules, provenance, and
            lifecycle history.
          </Text>
        </div>
        <Button size="xs" variant="light" color="gray" loading={loading} onClick={load}>
          Refresh
        </Button>
      </Group>
      <Alert color="blue" variant="light" title="Grant sources">
        Configuration-file access is durable and changes through reviewed deployment configuration. Database grants are
        operator-approved and may end at a deadline, release, or revocation. Denied requests create neither.
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
      {visible.map((item) => {
        const { grant } = item;
        const sourceId = grant.source.kind === "database" ? grant.source.id : grant.source.entry_id;
        return <GrantCard key={`${item.agent_id}:${sourceId}`} item={item} onRevoke={setRevoking} />;
      })}
      <RevokeDialog item={revoking} busy={revokeBusy} onClose={() => setRevoking(null)} onConfirm={confirmRevoke} />
    </Stack>
  );
}
