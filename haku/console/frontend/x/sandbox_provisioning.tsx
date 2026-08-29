import { Badge, Group, Loader, Table, Text } from "@mantine/core";

import type { Conversation } from "../client";
import { formatTimestamp } from "../time";

type Provisioning = NonNullable<Conversation["provisioning"]>;

const STEP_LABELS: Record<Provisioning["step"], string> = {
  claim_absent: "No SandboxClaim — never created, or already reclaimed",
  claim_created: "SandboxClaim created; nothing observed past it",
  waiting_for_sandbox: "Waiting for Sandbox assignment",
  waiting_for_pod: "Waiting for the sandbox Pod",
  waiting_for_pod_ready: "Waiting for the Pod and runner container",
  waiting_for_runner: "Pod is ready; waiting for the Claude bridge",
};

const STEP_SUMMARIES: Record<Provisioning["step"], string> = {
  claim_absent: "Claim absent",
  claim_created: "Claim created",
  waiting_for_sandbox: "Waiting for Sandbox",
  waiting_for_pod: "Waiting for Pod",
  waiting_for_pod_ready: "Waiting for Pod ready",
  waiting_for_runner: "Waiting for runner",
};

function readiness(value: boolean | null | undefined, pending: string): { color: string; label: string } {
  if (value === true) return { color: "teal", label: "ready" };
  if (value === false) return { color: "yellow", label: "not ready" };
  return { color: "gray", label: pending };
}

function inspectionAge(inspectedAt: string): { text: string; title: string } | null {
  const timestamp = formatTimestamp(inspectedAt);
  return timestamp.isFresh ? null : { text: `stale · ${timestamp.text}`, title: timestamp.title };
}

/** What Kubernetes says about a sandbox still coming up.
 *
 * Read live off the claim, the Sandbox, the Pod and the runner container rather than stored, so a
 * session that never comes up says which of the four it is stuck behind — which is the whole
 * account for a session that dies before the CLI produces a single frame.
 */
export function SandboxProvisioning({ provisioning }: { provisioning: Provisioning }): JSX.Element {
  const stale = inspectionAge(provisioning.inspected_at);
  return (
    <section className="haku-provisioning" aria-label="Sandbox provisioning">
      <Group className="haku-provisioning-header" justify="space-between" align="center" wrap="nowrap">
        <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
          <Loader size="xs" aria-label="Sandbox provisioning in progress" />
          <Text fw={600} size="sm" style={{ flex: "0 0 auto" }}>
            Sandbox provisioning
          </Text>
          <Text size="xs" c="dimmed" className="haku-provisioning-step" title={STEP_LABELS[provisioning.step]}>
            {STEP_SUMMARIES[provisioning.step]}
          </Text>
        </Group>
        {stale && (
          <Text size="xs" c="yellow" className="haku-provisioning-stale" title={stale.title}>
            {stale.text}
          </Text>
        )}
      </Group>
      <Table className="haku-provisioning-table" aria-label="Sandbox resources">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Resource</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Detail</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          <Resource
            label="SandboxClaim"
            name={provisioning.claim_name}
            readiness={readiness(provisioning.claim_ready, "pending")}
            detail={provisioning.claim_reason ?? provisioning.claim_message ?? undefined}
            detailTitle={provisioning.claim_message ?? undefined}
          />
          <Resource
            label="Sandbox"
            name={provisioning.sandbox_name}
            readiness={readiness(provisioning.sandbox_ready, "not assigned")}
          />
          <Resource
            label="Pod"
            name={provisioning.pod_name}
            readiness={readiness(provisioning.pod_ready, provisioning.pod_phase ?? "not created")}
            detail={provisioning.pod_phase ? `phase: ${provisioning.pod_phase}` : undefined}
          />
          <Resource
            label="Runner"
            readiness={readiness(provisioning.runner_ready, "not reported")}
            detail={provisioning.runner_state ?? undefined}
          />
          <Resource label="Claude bridge" readiness={{ color: "blue", label: "waiting" }} />
        </Table.Tbody>
      </Table>
      {provisioning.observation_error && (
        <Text c="red" size="xs" className="haku-provisioning-note">
          Observation failed · {provisioning.observation_error}
        </Text>
      )}
    </section>
  );
}

function Resource({
  label,
  name,
  readiness: state,
  detail,
  detailTitle,
}: {
  label: string;
  name?: string | null;
  readiness: { color: string; label: string };
  detail?: string;
  detailTitle?: string;
}) {
  return (
    <Table.Tr>
      <Table.Td data-slot="resource" className="haku-provisioning-resource">
        <Text size="xs" fw={600}>
          {label}
        </Text>
      </Table.Td>
      <Table.Td data-slot="status" className="haku-provisioning-status">
        <Badge color={state.color} variant="light" size="xs">
          {state.label}
        </Badge>
      </Table.Td>
      <Table.Td data-slot="detail" className="haku-provisioning-detail">
        {name ? (
          <Text size="xs" ff="monospace" className="haku-provisioning-name">
            <span title={name}>{name}</span>
          </Text>
        ) : (
          <Text size="xs" c="dimmed" className="haku-provisioning-detail-line" title={detailTitle ?? detail}>
            {detail ?? "—"}
          </Text>
        )}
        {name && detail && (
          <Text c="dimmed" size="xs" className="haku-provisioning-detail-line" title={detailTitle ?? detail}>
            {detail}
          </Text>
        )}
      </Table.Td>
    </Table.Tr>
  );
}
