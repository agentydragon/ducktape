import { type JSX, useEffect, useRef, useState } from "react";
/**
 * The lifecycle controls the sandbox list and the sandbox page share: the suspend/resume button,
 * and deletion, which the API takes only from a suspended sandbox
 * (`agentplane/runner_gateway/environments/internal/management/access.go`). The rule is restated here so the UI offers the
 * action when it will be accepted, not so it enforces anything.
 */
import { ActionIcon, Alert, Button, Checkbox, Group, List, Modal, Stack, Text, Tooltip } from "@mantine/core";
import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import type { Identity } from "../../../policy/v1/policy_service_pb";
import type { EnvironmentTarget } from "../../../proto/identity_pb";
import type { Session } from "../../../runner_gateway/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../client";
import { deleteIdentity, listIdentitySessions, type DeleteOutcome } from "../identities/client";
import { identityOrigin } from "../identities/origin";
import { remainingIdentityReferences } from "../identities/references";
import { useIdentities, usePolicyResources } from "../policy/watch";
import { sandboxInventoryFresh, useRequiredSandboxesLive } from "./live";
import { deleteEnvironment } from "./client";
import { sandboxOperatingMode, sandboxPhase } from "../sandbox_status";
import { IconPlayerPause, IconPlayerPlay } from "../icons";

export const DELETE_NEEDS_SUSPENDED = "Suspend the sandbox before deleting it";

/** Whether the API will accept a deletion: only a suspended sandbox may go. */
export function deletable(sandbox: EnvironmentDescriptor): boolean {
  return sandboxPhase(sandbox) === "Suspended";
}

/** Deletion was asked for: it takes no new work and cannot be suspended or resumed. */
export function deleting(sandbox: EnvironmentDescriptor): boolean {
  const phase = sandboxPhase(sandbox);
  return phase === "Quiescing" || phase === "Tearing down";
}

export function SuspendResume({
  sandbox,
  onAct,
}: {
  sandbox: EnvironmentDescriptor;
  onAct: (action: "suspend" | "resume") => void;
}): JSX.Element {
  const mode = sandboxOperatingMode(sandbox);
  const resume = mode === "Suspended";
  const modeUnknown = mode === null;
  const disabled = deleting(sandbox) || modeUnknown;
  const label = deleting(sandbox)
    ? "Sandbox deleting"
    : modeUnknown
      ? "Waiting for controller status"
      : resume
        ? "Resume"
        : "Suspend";
  return (
    <Tooltip label={label} withArrow>
      <ActionIcon
        variant="light"
        aria-label={modeUnknown ? "Sandbox mode unavailable" : resume ? "Resume" : "Suspend"}
        disabled={disabled}
        onClick={() => onAct(resume ? "resume" : "suspend")}
      >
        {resume ? <IconPlayerPlay size={16} /> : <IconPlayerPause size={16} />}
      </ActionIcon>
    </Tooltip>
  );
}

/** What deleting asks before it happens; rendered only while the question stands. */
export function ConfirmDelete({
  target,
  identityName,
  onCancel,
  onDeleted,
}: {
  target: EnvironmentTarget;
  identityName: string;
  onCancel: () => void;
  onDeleted: () => void;
}): JSX.Element {
  const [pending, setPending] = useState(false);
  const [sandboxDeleted, setSandboxDeleted] = useState(false);
  const [selected, setSelected] = useState(false);
  // No Identity currently carries the agreed app-minted origin marker, so cleanup starts off.
  const [failure, setFailure] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<DeleteOutcome | null>(null);
  const [deletingIdentity, setDeletingIdentity] = useState<Identity | null>(null);
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const inventory = useRequiredSandboxesLive();
  const { identities, error: identityError } = useIdentities();
  const identity = identities?.find((item) => item.identity?.name === identityName) ?? null;
  const policy = usePolicyResources(
    identity ? { case: "identity", value: identityName } : undefined,
    identity?.identity?.uid
  );
  const current = inventory.snapshot?.find(
    (item) =>
      item.target?.namespace === target.namespace && item.target.name === target.name && item.target.uid === target.uid
  );
  const inventoryReady = sandboxInventoryFresh(inventory) && current?.identityName === identityName;
  const references =
    identity && policy.resources && sessions && inventoryReady
      ? remainingIdentityReferences(identity, policy.resources.bindings, inventory.snapshot ?? [], sessions, target)
      : null;
  const canDeleteIdentity = identity !== null && identityOrigin(identity) !== "git" && references?.length === 0;
  const latest = useRef({ inventory, identities, identityError, identity, policy });
  latest.current = { inventory, identities, identityError, identity, policy };

  async function completePreflight(expected: Identity, excludingTarget: boolean): Promise<void> {
    const listed = await listIdentitySessions();
    const state = latest.current;
    if (
      state.identityError ||
      state.policy.error ||
      !state.identities ||
      !state.policy.resources ||
      !state.identity ||
      state.identity.identity?.uid !== expected.identity?.uid ||
      identityOrigin(state.identity) === "git" ||
      !sandboxInventoryFresh(state.inventory)
    ) {
      throw new Error("Identity references are unavailable; cleanup was not attempted");
    }
    const selectedSandbox = state.inventory.snapshot?.find(
      (item) =>
        item.target?.namespace === target.namespace &&
        item.target.name === target.name &&
        item.target.uid === target.uid
    );
    if (excludingTarget && selectedSandbox?.identityName !== identityName) {
      throw new Error("The selected Sandbox or its identity changed; cleanup was not attempted");
    }
    if (!excludingTarget && selectedSandbox) {
      throw new Error("Sandbox deletion has not completed in inventory; retry identity cleanup after it disappears");
    }
    const blockers = remainingIdentityReferences(
      expected,
      state.policy.resources.bindings,
      state.inventory.snapshot ?? [],
      listed,
      excludingTarget ? target : null
    );
    if (blockers.length) throw new Error(`Identity is still referenced by ${blockers.join(", ")}`);
  }

  useEffect(() => {
    if (!identityName) return;
    let active = true;
    void listIdentitySessions().then(
      (listed) => {
        if (active) setSessions(listed);
      },
      (reason: unknown) => {
        if (active) setSessionError(displayableError(reason));
      }
    );
    return () => {
      active = false;
    };
  }, [identityName]);

  async function confirm(): Promise<void> {
    if (pending || (selected && !canDeleteIdentity)) return;
    setPending(true);
    setFailure(null);
    try {
      if (selected && identity) await completePreflight(identity, true);
      await deleteEnvironment(target);
      setSandboxDeleted(true);
      if (selected && identity) {
        setDeletingIdentity(identity);
        try {
          await completePreflight(identity, false);
        } catch (reason: unknown) {
          setOutcome({ case: "failed", error: displayableError(reason) });
          return;
        }
        const result = await deleteIdentity(identity);
        if (result.case !== "deleted") {
          setOutcome(result);
          return;
        }
      }
      onDeleted();
    } catch (reason: unknown) {
      setFailure(displayableError(reason));
    } finally {
      setPending(false);
    }
  }

  async function retryIdentityDelete(): Promise<void> {
    if (pending || !deletingIdentity) return;
    setPending(true);
    try {
      await completePreflight(deletingIdentity, false);
      const result = await deleteIdentity(deletingIdentity);
      if (result.case === "deleted") onDeleted();
      else setOutcome(result);
    } catch (reason: unknown) {
      setOutcome({ case: "failed", error: displayableError(reason) });
    } finally {
      setPending(false);
    }
  }

  function close(): void {
    if (pending) return;
    if (sandboxDeleted) onDeleted();
    else onCancel();
  }
  return (
    <Modal opened onClose={close} title={`Delete ${target.name}?`}>
      <Stack>
        <Text size="sm">
          The Pod and its volume go with the sandbox, and everything written on it. Threads already recorded outlive it.
        </Text>
        {identityName && (
          <Stack gap="xs" component="section" aria-label="Identity cleanup">
            <Checkbox
              label={`Delete identity ${identityName} too`}
              description="The identity and bindings it owns have a separate lifetime from this Sandbox."
              checked={selected}
              disabled={!canDeleteIdentity || sandboxDeleted}
              onChange={(event) => setSelected(event.currentTarget.checked)}
            />
            {!canDeleteIdentity && !sandboxDeleted && (
              <Text size="sm" c="dimmed">
                {identityError ??
                  policy.error ??
                  sessionError ??
                  (identities && !identity
                    ? "The named identity is no longer present; cleanup is unavailable."
                    : references?.length
                      ? `Identity cleanup is unavailable while ${references.join(", ")} still references it.`
                      : identity && identityOrigin(identity) === "git"
                        ? "Flux manages this identity; remove it in the repository."
                        : "Checking the identity, Sandbox inventory, bindings and Sessions before offering cleanup.")}
              </Text>
            )}
          </Stack>
        )}
        {failure && (
          <Alert color="red" role="alert">
            {failure}
          </Alert>
        )}
        {outcome?.case === "failed" && (
          <Alert color="red" role="alert">
            Identity not deleted: {outcome.error}
          </Alert>
        )}
        {outcome?.case === "referenced" && (
          <Alert color="red" role="alert" title="The Policy Controller refused identity deletion">
            <List size="sm">
              {outcome.references.map((reference) => (
                <List.Item key={`${reference.kind}/${reference.name}`}>
                  {reference.kind} {reference.name}
                </List.Item>
              ))}
            </List>
          </Alert>
        )}
        {sandboxDeleted && outcome && (
          <Text size="sm">
            Sandbox deletion was requested. The identity remains; retry after the Sandbox disappears and its references
            are gone.
          </Text>
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={close} disabled={pending}>
            {sandboxDeleted ? "Done" : "Cancel"}
          </Button>
          <Button
            color="red"
            onClick={() => void (sandboxDeleted ? retryIdentityDelete() : confirm())}
            loading={pending}
            disabled={sandboxDeleted ? !deletingIdentity : selected && !canDeleteIdentity}
          >
            {sandboxDeleted ? "Retry identity deletion" : "Delete"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
