import { Badge, Box, Code, Group, Loader, Paper, Stack, Text } from "@mantine/core";

import type { ConversationSession } from "../client";

type Provisioning = NonNullable<ConversationSession["provisioning"]>;

const STEP_LABELS: Record<Provisioning["step"], string> = {
  claim_absent: "No SandboxClaim — never created, or already reclaimed",
  claim_created: "SandboxClaim created; nothing observed past it",
  waiting_for_sandbox: "Waiting for Sandbox assignment",
  waiting_for_pod: "Waiting for the sandbox Pod",
  waiting_for_pod_ready: "Waiting for the Pod and runner container",
  waiting_for_runner: "Pod is ready; waiting for the Claude bridge",
};

function readiness(value: boolean | null | undefined, pending: string): { color: string; label: string } {
  if (value === true) return { color: "teal", label: "ready" };
  if (value === false) return { color: "yellow", label: "not ready" };
  return { color: "gray", label: pending };
}

/** What Kubernetes says about a sandbox still coming up.
 *
 * Read live off the claim, the Sandbox, the Pod and the runner container rather than stored, so a
 * session that never comes up says which of the four it is stuck behind — which is the whole
 * account for a session that dies before the CLI produces a single frame.
 */
export function SandboxProvisioning({ provisioning }: { provisioning: Provisioning }) {
  return (
    <Paper withBorder p="md">
      <Stack gap="md">
        <Group gap="sm">
          <Loader size="sm" />
          <div>
            <Text fw={600} size="sm">
              {STEP_LABELS[provisioning.step]}
            </Text>
            <Text c="dimmed" size="xs">
              Live state from the Agent Sandbox resources; waiting for the runner to connect.
            </Text>
          </div>
        </Group>
        <Stack gap="xs">
          <Resource
            label="SandboxClaim"
            name={provisioning.claim_name}
            readiness={readiness(provisioning.claim_ready, "pending")}
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
            label="runner container"
            readiness={readiness(provisioning.runner_ready, "not reported")}
            detail={provisioning.runner_state ?? undefined}
          />
          <Resource label="Claude bridge" readiness={{ color: "blue", label: "waiting" }} />
          {(provisioning.claim_reason || provisioning.claim_message) && (
            <Text c="dimmed" size="xs">
              Claim: {[provisioning.claim_reason, provisioning.claim_message].filter(Boolean).join(" — ")}
            </Text>
          )}
          {provisioning.observation_error && (
            <Text c="red" size="xs">
              Kubernetes observation failed: {provisioning.observation_error}
            </Text>
          )}
          <Text c="dimmed" size="xs">
            Observed {new Date(provisioning.inspected_at).toLocaleTimeString()}
          </Text>
        </Stack>
      </Stack>
    </Paper>
  );
}

function Resource({
  label,
  name,
  readiness: state,
  detail,
}: {
  label: string;
  name?: string | null;
  readiness: { color: string; label: string };
  detail?: string;
}) {
  const badge = (
    <Badge color={state.color} variant="light" size="sm">
      {state.label}
    </Badge>
  );
  const value = (
    <Stack gap={0} style={{ minWidth: 0 }}>
      {name && (
        <Code block style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {name}
        </Code>
      )}
      {detail && (
        <Text c="dimmed" size="xs">
          {detail}
        </Text>
      )}
    </Stack>
  );
  return (
    <>
      <Box hiddenFrom="sm">
        <Stack gap={2}>
          <Group justify="space-between" wrap="nowrap">
            <Text size="xs" fw={600}>
              {label}
            </Text>
            {badge}
          </Group>
          {value}
        </Stack>
      </Box>
      <Box visibleFrom="sm">
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(7.5rem, auto) minmax(0, 1fr) auto",
            alignItems: "center",
            columnGap: "0.5rem",
          }}
        >
          <Text size="xs" fw={600}>
            {label}
          </Text>
          {value}
          {badge}
        </div>
      </Box>
    </>
  );
}
