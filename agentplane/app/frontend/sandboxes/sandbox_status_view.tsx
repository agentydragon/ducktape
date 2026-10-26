import { Accordion, Badge, Button, Group, Paper, Stack, Table, Text, Title } from "@mantine/core";
import { type JSX, type ReactNode, useEffect, useState } from "react";

import {
  displayableError,
  getRunnerStatus,
  harnessLabel,
  type RunnerStatusView,
  type SandboxView,
  type ThreadView,
} from "../client";
import { HelpTooltip } from "../help_tooltip";
import { IdleSuspendInput } from "./idle_suspend";
import { JsonView } from "../json_view";
import { deleting } from "./lifecycle";
import { RawSwitch } from "../raw_switch";
import {
  containerStatuses,
  podIp,
  podPhase,
  rawConditions,
  rawString,
  sandboxSummary,
  type ContainerStatus,
  type RawCondition,
} from "../sandbox_status";
import { TimeAgo } from "../time_ago";
import { WakeOnSendCheckbox } from "./wake_on_send";

function Ago({ value }: { value: string | null }): JSX.Element {
  return value ? <TimeAgo value={value} /> : <>—</>;
}

function ConditionsTable({ conditions }: { conditions: RawCondition[] }): JSX.Element {
  return (
    <Table.ScrollContainer minWidth={620} type="native">
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Condition</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Reason</Table.Th>
            <Table.Th>Message</Table.Th>
            <Table.Th>Transition</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {conditions.map((condition) => (
            <Table.Tr key={condition.type}>
              <Table.Td>{condition.type}</Table.Td>
              <Table.Td>
                <Text span c={condition.status === "True" ? "green" : "orange"}>
                  {condition.status}
                </Text>
              </Table.Td>
              <Table.Td>{condition.reason ?? "—"}</Table.Td>
              <Table.Td>{condition.message ?? "—"}</Table.Td>
              <Table.Td>
                <Ago value={condition.lastTransitionTime} />
                {condition.lastProbeTime && (
                  <>
                    {" "}
                    · probed <TimeAgo value={condition.lastProbeTime} />
                  </>
                )}
                {condition.observedGeneration !== null && ` · generation ${condition.observedGeneration}`}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

function ContainersTable({ containers }: { containers: ContainerStatus[] }): JSX.Element {
  return (
    <Table.ScrollContainer minWidth={620} type="native">
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Container</Table.Th>
            <Table.Th>State</Table.Th>
            <Table.Th>Ready</Table.Th>
            <Table.Th>Restarts</Table.Th>
            <Table.Th>Detail</Table.Th>
            <Table.Th>Started</Table.Th>
            <Table.Th>Finished</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {containers.map((container) => (
            <Table.Tr key={`${container.role}/${container.name}`}>
              <Table.Td>
                {container.role === "container" ? container.name : `${container.role} ${container.name}`}
              </Table.Td>
              <Table.Td>
                <Text span c={container.state === "running" ? "green" : "orange"}>
                  {container.state}
                </Text>
              </Table.Td>
              <Table.Td>{container.ready ? "yes" : "no"}</Table.Td>
              <Table.Td>
                <Text span c={container.restartCount > 0 ? "red" : undefined}>
                  {container.restartCount}
                </Text>
              </Table.Td>
              <Table.Td>
                {[container.reason, container.message, container.exitCode !== null && `exit ${container.exitCode}`]
                  .filter(Boolean)
                  .join(" · ") || "—"}
              </Table.Td>
              <Table.Td>
                <Ago value={container.startedAt} />
              </Table.Td>
              <Table.Td>
                <Ago value={container.finishedAt} />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

function conditionsSummary(conditions: RawCondition[]): string {
  if (conditions.length === 0) return "none reported";
  const unmet = conditions.filter((condition) => condition.status !== "True");
  return unmet.length === 0
    ? `all ${conditions.length} True`
    : unmet.map((condition) => `${condition.type} ${condition.status}`).join(", ");
}

function containersSummary(containers: ContainerStatus[]): string {
  return `${containers.filter((container) => container.ready).length} of ${containers.length} ready`;
}

function StatusItem({
  value,
  title,
  summary,
  children,
}: {
  value: string;
  title: string;
  summary: string;
  children: ReactNode;
}): JSX.Element {
  return (
    <Accordion.Item value={value}>
      <Accordion.Control>
        <Group gap="xs">
          <Text size="sm" fw={600}>
            {title}
          </Text>
          <Text size="xs" c="dimmed">
            {summary}
          </Text>
        </Group>
      </Accordion.Control>
      <Accordion.Panel>{children}</Accordion.Panel>
    </Accordion.Item>
  );
}

/**
 * What Kubernetes says about the Sandbox: its own status conditions, then its Pod's conditions and
 * containers. An item starts open when something in it is not as it should be.
 */
function KubernetesStatus({ sandbox }: { sandbox: SandboxView }): JSX.Element {
  const sandboxConditions = rawConditions(sandbox.status);
  const podConditions = rawConditions(sandbox.pod?.status ?? null);
  const containers = containerStatuses(sandbox.pod?.status ?? null);
  const unmet = (conditions: RawCondition[]): boolean => conditions.some((condition) => condition.status !== "True");
  const open = [
    ...(unmet(sandboxConditions) ? ["sandbox"] : []),
    ...(unmet(podConditions) ? ["pod"] : []),
    ...(containers.some((container) => !container.ready) ? ["containers"] : []),
  ];
  return (
    <Accordion multiple defaultValue={open}>
      <StatusItem value="sandbox" title="Sandbox conditions" summary={conditionsSummary(sandboxConditions)}>
        {sandboxConditions.length > 0 ? (
          <ConditionsTable conditions={sandboxConditions} />
        ) : (
          <Text size="sm" c="dimmed">
            No Sandbox conditions reported.
          </Text>
        )}
      </StatusItem>
      {sandbox.pod && (
        <>
          <StatusItem value="pod" title="Pod conditions" summary={conditionsSummary(podConditions)}>
            {podConditions.length > 0 ? (
              <ConditionsTable conditions={podConditions} />
            ) : (
              <Text size="sm" c="dimmed">
                No Pod conditions reported.
              </Text>
            )}
          </StatusItem>
          {containers.length > 0 && (
            <StatusItem value="containers" title="Containers" summary={containersSummary(containers)}>
              <ContainersTable containers={containers} />
            </StatusItem>
          )}
        </>
      )}
    </Accordion>
  );
}

function Detail({ label, help, children }: { label: string; help?: string; children: ReactNode }): JSX.Element {
  return (
    <Table.Tr>
      <Table.Th scope="row" aria-label={label} style={{ width: "1%", whiteSpace: "nowrap", verticalAlign: "top" }}>
        <Group gap={4} wrap="nowrap">
          {label}
          {help && <HelpTooltip topic={label} description={help} />}
        </Group>
      </Table.Th>
      <Table.Td>{children}</Table.Td>
    </Table.Tr>
  );
}

/** The Sandbox as a whole: where it stands, what it runs as, and the two settings an operator changes. */
function SandboxDetails({
  sandbox,
  onError,
}: {
  sandbox: SandboxView;
  onError: (message: string | null) => void;
}): JSX.Element {
  const pod = sandbox.pod;
  const startedAt = rawString(pod?.status?.startTime);
  return (
    <Table>
      <Table.Tbody>
        <Detail label="State">{sandboxSummary(sandbox).label}</Detail>
        <Detail label="Created">
          <TimeAgo value={sandbox.created_at} />
        </Detail>
        {sandbox.preset && <Detail label="Preset">{sandbox.preset}</Detail>}
        <Detail
          label="ServiceAccount"
          help="The subject its policy bindings name. Two sandboxes sharing an account share what they may do."
        >
          {sandbox.service_account.namespace}/{sandbox.service_account.name}
        </Detail>
        <Detail label="Wake on message" help="A message to one of its Sessions resumes it while suspended.">
          <WakeOnSendCheckbox sandbox={sandbox} disabled={deleting(sandbox)} onError={onError} />
        </Detail>
        <Detail label="Suspend when idle (min)">
          <IdleSuspendInput sandbox={sandbox} disabled={deleting(sandbox)} onError={onError} />
          <Text size="xs" c="dimmed">
            Empty means never.
          </Text>
        </Detail>
        <Detail label="Pod">
          {pod ? (
            <>
              {podPhase(sandbox) ?? "phase unknown"}
              {podIp(sandbox) ? ` at ${podIp(sandbox)}` : ""}
              {pod.node_name ? ` on ${pod.node_name}` : ", not placed"}
              {pod.deleting ? " · deleting" : ""}
              {rawString(pod.status?.reason) ? `: ${rawString(pod.status?.reason)}` : ""}
              {rawString(pod.status?.message) ? ` (${rawString(pod.status?.message)})` : ""}
              <Text size="xs" c="dimmed">
                {pod.namespace}/{pod.name} · UID {pod.uid}
                {startedAt && (
                  <>
                    {" "}
                    · started <TimeAgo value={startedAt} />
                  </>
                )}
                {rawString(pod.status?.qosClass) ? ` · QoS ${rawString(pod.status?.qosClass)}` : ""}
              </Text>
            </>
          ) : (
            "None"
          )}
        </Detail>
      </Table.Tbody>
    </Table>
  );
}

function processStateLabel(state: RunnerStatusView["processes"][number]["state"]): string {
  switch (state) {
    case "PROCESS_STATE_STARTING":
      return "Starting";
    case "PROCESS_STATE_RUNNING":
      return "Running";
    case "PROCESS_STATE_STOPPING":
      return "Stopping";
    case "PROCESS_STATE_UNSPECIFIED":
      return "Unspecified";
  }
}

function executionStateLabel(state: RunnerStatusView["sessions"][number]["execution_state"]): string {
  switch (state) {
    case "SESSION_EXECUTION_STATE_DORMANT":
      return "Dormant";
    case "SESSION_EXECUTION_STATE_PREPARING":
      return "Preparing";
    case "SESSION_EXECUTION_STATE_READY":
      return "Ready";
    case "SESSION_EXECUTION_STATE_FAILED":
      return "Failed";
    case "SESSION_EXECUTION_STATE_UNSPECIFIED":
      return "Unspecified";
  }
}

type RunnerDiagnosticsState =
  | { kind: "loading" }
  | { kind: "available"; value: RunnerStatusView; observedAt: string }
  | { kind: "unavailable"; reason: string }
  | { kind: "suspended" };

function RunnerDiagnostics({
  sandbox,
  threads,
  active,
}: {
  sandbox: SandboxView;
  threads: ThreadView[];
  active: boolean;
}): JSX.Element {
  const [state, setState] = useState<RunnerDiagnosticsState>({ kind: "loading" });
  const [refreshVersion, setRefreshVersion] = useState(0);
  useEffect(() => {
    if (!active) return;
    if (sandbox.phase === "Suspended" || sandbox.operating_mode === "Suspended") {
      setState({ kind: "suspended" });
      return;
    }
    let current = true;
    setState({ kind: "loading" });
    void getRunnerStatus(sandbox.name).then(
      (value) => {
        if (current) setState({ kind: "available", value, observedAt: new Date().toISOString() });
      },
      (reason: unknown) => {
        if (current) setState({ kind: "unavailable", reason: displayableError(reason) });
      }
    );
    return () => {
      current = false;
    };
  }, [active, refreshVersion, sandbox.name, sandbox.operating_mode, sandbox.phase]);
  const threadsById = new Map(threads.map((thread) => [thread.id, thread]));

  return (
    <Stack gap="xs" data-testid="sandbox-runner-diagnostics">
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="xs">
        <div>
          <Title order={5}>Runner diagnostics</Title>
          <Text size="xs" c="dimmed">
            Read-only snapshot of process and Session state reported by the runner.
          </Text>
        </div>
        <Button
          size="xs"
          variant="subtle"
          disabled={state.kind === "loading" || state.kind === "suspended"}
          onClick={() => setRefreshVersion((version) => version + 1)}
        >
          Refresh
        </Button>
      </Group>
      {state.kind === "loading" && (
        <Text size="sm" role="status" c="dimmed">
          Loading runner diagnostics…
        </Text>
      )}
      {state.kind === "suspended" && (
        <Text size="sm" role="status" c="dimmed">
          Sandbox suspended. Runner diagnostics are unavailable until it is running.
        </Text>
      )}
      {state.kind === "unavailable" && (
        <Text size="sm" role="status" c="dimmed">
          Runner diagnostics unavailable: {state.reason}
        </Text>
      )}
      {state.kind === "available" && (
        <Stack gap="xs">
          <Text size="xs" c="dimmed">
            Snapshot read <TimeAgo value={state.observedAt} />
          </Text>
          <Text size="sm">Runner boot ID · {state.value.runner_boot_id}</Text>
          {state.value.processes.length === 0 ? (
            <Text size="sm" c="dimmed">
              No harness processes reported.
            </Text>
          ) : (
            <Stack gap="xs">
              <Title order={6}>Harness processes</Title>
              {state.value.processes.map((process) => (
                <Paper key={process.process_id} withBorder p="sm" radius="md">
                  <Stack gap={4}>
                    <Group justify="space-between" wrap="wrap" gap="xs">
                      <Text fw={600}>{harnessLabel(process.harness)}</Text>
                      <Badge>{processStateLabel(process.state)}</Badge>
                    </Group>
                    <Text size="sm">PID {process.pid ?? "not started"}</Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Process ID · {process.process_id}
                    </Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Assigned Sessions ·{" "}
                      {process.assigned_session_ids.length ? process.assigned_session_ids.join(", ") : "None"}
                    </Text>
                  </Stack>
                </Paper>
              ))}
            </Stack>
          )}
          {state.value.sessions.length === 0 ? (
            <Text size="sm" c="dimmed">
              No Sessions reported.
            </Text>
          ) : (
            <Stack gap="xs">
              <Title order={6}>Sessions</Title>
              {state.value.sessions.map((session) => {
                const title = threadsById.get(session.session_id)?.name?.trim();
                return (
                  <Paper key={session.session_id} withBorder p="sm" radius="md">
                    <Stack gap={4}>
                      <Group justify="space-between" wrap="wrap" gap="xs">
                        <Text fw={600} style={{ overflowWrap: "anywhere" }}>
                          {title || session.session_id}
                        </Text>
                        <Badge>{executionStateLabel(session.execution_state)}</Badge>
                      </Group>
                      {title && (
                        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                          Session ID · {session.session_id}
                        </Text>
                      )}
                      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                        Process ID · {session.process_id ?? "—"}
                      </Text>
                      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                        Native Session ID · {session.native_session_id ?? "—"}
                      </Text>
                      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                        Active turn · {session.active_turn_id ?? "—"}
                      </Text>
                      <Text size="xs" c="dimmed">
                        Failure cursor · {session.failure_cursor ?? "—"}
                        {session.busy ? " · Work in progress" : ""}
                      </Text>
                    </Stack>
                  </Paper>
                );
              })}
            </Stack>
          )}
        </Stack>
      )}
    </Stack>
  );
}

/** The Sandbox's state and settings, what Kubernetes reports of it, and what its runner reports. */
export function StatusView({
  sandbox,
  threads,
  active,
  onError,
}: {
  sandbox: SandboxView;
  threads: ThreadView[];
  active: boolean;
  onError: (message: string | null) => void;
}): JSX.Element {
  const [raw, setRaw] = useState(false);
  return (
    <Stack gap="md">
      <Group justify="flex-end">
        <RawSwitch raw={raw} onChange={setRaw} />
      </Group>
      {raw ? (
        <JsonView value={sandbox} />
      ) : (
        <>
          <SandboxDetails sandbox={sandbox} onError={onError} />
          <KubernetesStatus sandbox={sandbox} />
        </>
      )}
      <RunnerDiagnostics sandbox={sandbox} threads={threads} active={active} />
    </Stack>
  );
}
