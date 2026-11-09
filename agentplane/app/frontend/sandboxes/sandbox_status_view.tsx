import { Accordion, Badge, Button, Group, Paper, Stack, Table, Text, Title } from "@mantine/core";
import { create, toJson } from "@bufbuild/protobuf";
import { timestampDate } from "@bufbuild/protobuf/wkt";
import { type JSX, type ReactNode, useEffect, useMemo, useState } from "react";

import type { ThreadView } from "../threads/client";
import { EnvironmentTargetSchema } from "../../../proto/identity_pb";
import {
  EnvironmentDescriptorSchema,
  OperatingMode,
  type EnvironmentDescriptor,
  SandboxPhase,
} from "../../../environments/v1/environment_service_pb";

import { displayableError } from "../client";
import { getEnvironmentRuntimeStatus } from "./client";
import { harnessLabel } from "../model_catalog";
import {
  EnvironmentRuntimeLifecycleBlockerKind,
  EnvironmentRuntimeLifecycleMode,
  EnvironmentRuntimeLifecycleStage,
  type Harness as ProtoHarness,
  ProcessState,
  SessionExecutionState,
  type EnvironmentRuntimeLifecycleStatus,
  type EnvironmentRuntimeStatus,
} from "../../../proto/session_pb";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import { IdleSuspendInput } from "./idle_suspend";
import { JsonView } from "../../../../util/frontend/json_view";
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
function KubernetesStatus({ sandbox }: { sandbox: EnvironmentDescriptor }): JSX.Element {
  const sandboxConditions = rawConditions(sandbox.kubernetesDiagnostics?.sandboxStatus);
  const podConditions = rawConditions(sandbox.kubernetesDiagnostics?.pod?.status ?? null);
  const containers = containerStatuses(sandbox.kubernetesDiagnostics?.pod?.status ?? null);
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
      {sandbox.kubernetesDiagnostics?.pod && (
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
  sandbox: EnvironmentDescriptor;
  onError: (message: string | null) => void;
}): JSX.Element {
  const pod = sandbox.kubernetesDiagnostics?.pod;
  const createdAt = sandbox.observedState?.createdAt;
  const startedAt = rawString(pod?.status?.startTime);
  return (
    <Table>
      <Table.Tbody>
        <Detail label="State">{sandboxSummary(sandbox).label}</Detail>
        <Detail label="Created">{createdAt ? <TimeAgo value={timestampDate(createdAt)} /> : "—"}</Detail>
        {sandbox.configuration?.preset && <Detail label="Preset">{sandbox.configuration?.preset}</Detail>}
        <Detail
          label="ServiceAccount"
          help="The subject its policy bindings name. Two sandboxes sharing an account share what they may do."
        >
          {sandbox.executionAccount?.account?.namespace ?? sandbox.target!.namespace}/
          {sandbox.executionAccount?.account?.name ?? sandbox.target!.name}
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
              {pod.nodeName ? ` on ${pod.nodeName}` : ", not placed"}
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

function processStateLabel(state: ProcessState): string {
  switch (state) {
    case ProcessState.STARTING:
      return "Starting";
    case ProcessState.RUNNING:
      return "Running";
    case ProcessState.STOPPING:
      return "Stopping";
    case ProcessState.UNSPECIFIED:
      return "Unspecified";
  }
  return "Unknown";
}

function executionStateLabel(state: SessionExecutionState): string {
  switch (state) {
    case SessionExecutionState.DORMANT:
      return "Dormant";
    case SessionExecutionState.PREPARING:
      return "Preparing";
    case SessionExecutionState.READY:
      return "Ready";
    case SessionExecutionState.FAILED:
      return "Failed";
    case SessionExecutionState.UNSPECIFIED:
      return "Unspecified";
  }
  return "Unknown";
}

function lifecycleModeLabel(mode: EnvironmentRuntimeLifecycleStatus["desiredMode"]): string {
  switch (mode) {
    case EnvironmentRuntimeLifecycleMode.RUNNING:
      return "Running";
    case EnvironmentRuntimeLifecycleMode.QUIESCING:
      return "Quiescing";
    case EnvironmentRuntimeLifecycleMode.CLOSING:
      return "Closing";
    case EnvironmentRuntimeLifecycleMode.UNSPECIFIED:
      return "Unknown";
  }
  return "Unknown";
}

function lifecycleStageLabel(stage: EnvironmentRuntimeLifecycleStatus["stage"]): string {
  switch (stage) {
    case EnvironmentRuntimeLifecycleStage.RUNNING:
      return "Running";
    case EnvironmentRuntimeLifecycleStage.DRAINING:
      return "Draining admissions";
    case EnvironmentRuntimeLifecycleStage.HALTING:
      return "Halting Sessions";
    case EnvironmentRuntimeLifecycleStage.STOPPING:
      return "Stopping processes";
    case EnvironmentRuntimeLifecycleStage.FENCING:
      return "Fencing processes";
    case EnvironmentRuntimeLifecycleStage.SEALING:
      return "Sealing journals";
    case EnvironmentRuntimeLifecycleStage.FLUSHING:
      return "Flushing history";
    case EnvironmentRuntimeLifecycleStage.QUIESCED:
      return "Quiesced";
    case EnvironmentRuntimeLifecycleStage.CLOSED:
      return "Closed";
    case EnvironmentRuntimeLifecycleStage.UNSPECIFIED:
      return "Unknown";
  }
  return "Unknown";
}

function lifecycleBlockerLabel(kind: EnvironmentRuntimeLifecycleBlockerKind): string {
  switch (kind) {
    case EnvironmentRuntimeLifecycleBlockerKind.ADMISSIONS:
      return "command admission";
    case EnvironmentRuntimeLifecycleBlockerKind.SESSION_HALT:
      return "Session halt";
    case EnvironmentRuntimeLifecycleBlockerKind.SESSION_PROCESS:
      return "Session process stop";
    case EnvironmentRuntimeLifecycleBlockerKind.CODEX_APP_SERVER:
      return "Codex app-server stop";
    case EnvironmentRuntimeLifecycleBlockerKind.CODEX_EXEC_SERVER:
      return "Codex exec-server fence";
    case EnvironmentRuntimeLifecycleBlockerKind.BOOTSTRAP:
      return "Sandbox bootstrap";
    case EnvironmentRuntimeLifecycleBlockerKind.PROCESS_CLEANUP:
      return "native process cleanup";
    case EnvironmentRuntimeLifecycleBlockerKind.SESSION_INSPECTION:
      return "Session inspection";
    case EnvironmentRuntimeLifecycleBlockerKind.JOURNAL_SEAL:
      return "Session journal seal";
    case EnvironmentRuntimeLifecycleBlockerKind.INGRESS_ACKNOWLEDGEMENT:
      return "History acknowledgement";
    case EnvironmentRuntimeLifecycleBlockerKind.UNSPECIFIED:
      return "unknown barrier";
  }
  return "unknown barrier";
}

function runtimeHarnessLabel(harness: ProtoHarness): string {
  return harnessLabel(harness);
}

function EnvironmentRuntimeLifecycleCard({ lifecycle }: { lifecycle: EnvironmentRuntimeLifecycleStatus }): JSX.Element {
  const blocker = lifecycle.blocker;
  const stage = lifecycleStageLabel(lifecycle.stage);
  const progress = !lifecycle.progressAvailable
    ? `Progress not published for generation ${lifecycle.desiredGeneration}.`
    : lifecycle.progressCurrent
      ? `Progress · ${stage}`
      : `Last progress · generation ${lifecycle.progressGeneration} · ${stage}`;

  return (
    <Paper data-testid="environment-runtime-lifecycle" withBorder p="sm" radius="md">
      <Stack gap={4}>
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Title order={6}>Lifecycle</Title>
          <Badge>{lifecycleModeLabel(lifecycle.desiredMode)}</Badge>
        </Group>
        <Text size="sm">
          Desired generation {lifecycle.desiredGeneration} · {progress}
        </Text>
        {lifecycle.progressAvailable && !lifecycle.progressCurrent && (
          <Text size="xs" c="orange">
            Waiting for progress from the desired generation.
          </Text>
        )}
        {blocker && (
          <>
            <Text size="sm" c="orange">
              Blocked at {lifecycleBlockerLabel(blocker.kind)}
              {blocker.resourceId && ` · ${blocker.resourceId}`}
            </Text>
            {blocker.resourceType && (
              <Text size="xs" c="dimmed">
                Resource · {blocker.resourceType} · {blocker.resourcePresent ? "present" : "missing"}
              </Text>
            )}
            {blocker.processId && (
              <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                Process · {blocker.processId}
              </Text>
            )}
            {blocker.error && (
              <Text size="sm" c="red" style={{ overflowWrap: "anywhere" }}>
                Resource error · {blocker.error}
              </Text>
            )}
            {blocker.kind === EnvironmentRuntimeLifecycleBlockerKind.ADMISSIONS && (
              <Text size="xs" c="dimmed">
                In-flight admissions · {blocker.admissions}
              </Text>
            )}
            {blocker.kind === EnvironmentRuntimeLifecycleBlockerKind.JOURNAL_SEAL && (
              <Text size="xs" c="dimmed">
                Journal seal · {blocker.journalSealPresent ? blocker.journalSealUid || "present" : "not present"}
                {blocker.expectedJournalSealUid && ` · expected ${blocker.expectedJournalSealUid}`}
              </Text>
            )}
            {blocker.kind === EnvironmentRuntimeLifecycleBlockerKind.INGRESS_ACKNOWLEDGEMENT && (
              <Text size="xs" c="dimmed">
                {blocker.acknowledgementPresent
                  ? `Acknowledged cursor · ${blocker.acknowledgementCursor} of ${blocker.journalSealCursor} · sealed ${blocker.acknowledgementSealed ? "yes" : "no"}`
                  : `No acknowledgement · final cursor ${blocker.journalSealCursor}`}
              </Text>
            )}
            {blocker.kind === EnvironmentRuntimeLifecycleBlockerKind.INGRESS_ACKNOWLEDGEMENT &&
              blocker.expectedJournalSealUid &&
              blocker.journalSealUid !== blocker.expectedJournalSealUid && (
                <Text size="xs" c="dimmed">
                  Journal seal · {blocker.journalSealPresent ? blocker.journalSealUid || "present" : "not present"}
                  {` · expected ${blocker.expectedJournalSealUid}`}
                </Text>
              )}
            {blocker.finalizersPending && (
              <Text size="xs" c="dimmed">
                Native process cleanup finalizer is still present.
              </Text>
            )}
          </>
        )}
        {lifecycle.error && lifecycle.error !== blocker?.error && (
          <Text size="sm" c="red" style={{ overflowWrap: "anywhere" }}>
            Lifecycle error · {lifecycle.error}
          </Text>
        )}
      </Stack>
    </Paper>
  );
}

type EnvironmentRuntimeDiagnosticsState =
  | { kind: "loading" }
  | { kind: "available"; value: EnvironmentRuntimeStatus; observedAt: string }
  | { kind: "unavailable"; reason: string }
  | { kind: "suspended" };

function EnvironmentRuntimeDiagnostics({
  sandbox,
  threads,
  active,
}: {
  sandbox: EnvironmentDescriptor;
  threads: ThreadView[];
  active: boolean;
}): JSX.Element {
  const [state, setState] = useState<EnvironmentRuntimeDiagnosticsState>({ kind: "loading" });
  const [refreshVersion, setRefreshVersion] = useState(0);
  const { namespace: sandboxNamespace, name: sandboxName, uid: sandboxUID } = sandbox.target!;
  const target = useMemo(
    () => create(EnvironmentTargetSchema, { namespace: sandboxNamespace, name: sandboxName, uid: sandboxUID }),
    [sandboxNamespace, sandboxName, sandboxUID]
  );
  useEffect(() => {
    if (!active) return;
    if (
      sandbox.observedState?.phase === SandboxPhase.SUSPENDED ||
      sandbox.observedState?.operatingMode === OperatingMode.SUSPENDED
    ) {
      setState({ kind: "suspended" });
      return;
    }
    let current = true;
    setState({ kind: "loading" });
    void getEnvironmentRuntimeStatus(target).then(
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
  }, [active, refreshVersion, target, sandbox.observedState?.operatingMode, sandbox.observedState?.phase]);
  const threadsById = new Map(threads.map((thread) => [thread.metadata.threadId, thread]));

  return (
    <Stack gap="xs" data-testid="sandbox-environment-runtime-diagnostics">
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="xs">
        <div>
          <Title order={5}>Runtime diagnostics</Title>
          <Text size="xs" c="dimmed">
            Read-only snapshot of Runtime lifecycle, process, and Session state.
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
          Loading Runtime diagnostics…
        </Text>
      )}
      {state.kind === "suspended" && (
        <Text size="sm" role="status" c="dimmed">
          Sandbox suspended. Runtime diagnostics are unavailable until it is running.
        </Text>
      )}
      {state.kind === "unavailable" && (
        <Text size="sm" role="status" c="dimmed">
          Runtime diagnostics unavailable: {state.reason}
        </Text>
      )}
      {state.kind === "available" && (
        <Stack gap="xs">
          <Text size="xs" c="dimmed">
            Snapshot read <TimeAgo value={state.observedAt} />
          </Text>
          <Text size="sm">Environment Runtime boot ID · {state.value.environmentRuntimeBootId}</Text>
          {state.value.lifecycle ? (
            <EnvironmentRuntimeLifecycleCard lifecycle={state.value.lifecycle} />
          ) : (
            <Text size="sm" c="dimmed">
              Runtime lifecycle progress is not reported.
            </Text>
          )}
          {state.value.processes.length === 0 ? (
            <Text size="sm" c="dimmed">
              No harness processes reported.
            </Text>
          ) : (
            <Stack gap="xs">
              <Title order={6}>Harness processes</Title>
              {state.value.processes.map((process) => (
                <Paper key={process.processId} withBorder p="sm" radius="md">
                  <Stack gap={4}>
                    <Group justify="space-between" wrap="wrap" gap="xs">
                      <Text fw={600}>{runtimeHarnessLabel(process.harness)}</Text>
                      <Badge>{processStateLabel(process.state)}</Badge>
                    </Group>
                    <Text size="sm">PID {process.pid === undefined ? "not started" : process.pid.toString()}</Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Process ID · {process.processId}
                    </Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Assigned Sessions ·{" "}
                      {process.assignedSessionIds.length ? process.assignedSessionIds.join(", ") : "None"}
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
                const title = threadsById.get(session.sessionId)?.metadata.name?.trim();
                return (
                  <Paper key={session.sessionId} withBorder p="sm" radius="md">
                    <Stack gap={4}>
                      <Group justify="space-between" wrap="wrap" gap="xs">
                        <Text fw={600} style={{ overflowWrap: "anywhere" }}>
                          {title || session.sessionId}
                        </Text>
                        <Badge>{executionStateLabel(session.executionState)}</Badge>
                      </Group>
                      {title && (
                        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                          Session ID · {session.sessionId}
                        </Text>
                      )}
                      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                        Process ID · {session.processId ?? "—"}
                      </Text>
                      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                        Native Session ID · {session.nativeSessionId ?? "—"}
                      </Text>
                      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                        Active turn · {session.activeTurnId ?? "—"}
                      </Text>
                      <Text size="xs" c="dimmed">
                        Failure cursor · {session.failureCursor?.toString() ?? "—"}
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
  sandbox: EnvironmentDescriptor;
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
        <JsonView value={toJson(EnvironmentDescriptorSchema, sandbox)} />
      ) : (
        <>
          <SandboxDetails sandbox={sandbox} onError={onError} />
          <KubernetesStatus sandbox={sandbox} />
        </>
      )}
      <EnvironmentRuntimeDiagnostics sandbox={sandbox} threads={threads} active={active} />
    </Stack>
  );
}
