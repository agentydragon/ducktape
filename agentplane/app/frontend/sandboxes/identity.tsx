import { Alert, Anchor, Button, Group, Select, Stack, Text, Title } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import { displayableError, HttpError } from "../client";
import { useIdentities } from "../policy/watch";
import { getEnvironmentIdentityRecord, setEnvironmentIdentity } from "./client";

function apiMessage(reason: unknown): string {
  if (
    reason instanceof HttpError &&
    typeof reason.body === "object" &&
    reason.body !== null &&
    "message" in reason.body &&
    typeof reason.body.message === "string"
  ) {
    return reason.body.message;
  }
  return displayableError(reason);
}

export function EnvironmentIdentity({ sandbox }: { sandbox: EnvironmentDescriptor }): JSX.Element {
  const target = sandbox.target;
  const [record, setRecord] = useState<{ target: string; name: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [selection, setSelection] = useState<string | null>(sandbox.identityName || null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const identities = useIdentities();
  const key = target
    ? `${target.namespace}/${target.name}/${target.uid}/${sandbox.observedState?.runtimeIncarnationUid ?? ""}`
    : "";
  const stopped = sandbox.observedState?.stopStage === "Stopped";

  useEffect(() => {
    setSelection(sandbox.identityName || null);
  }, [sandbox.identityName]);

  async function save(): Promise<void> {
    if (!target || !stopped || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await setEnvironmentIdentity(target, selection);
    } catch (reason: unknown) {
      setSaveError(apiMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    if (!target) return;
    let active = true;
    void getEnvironmentIdentityRecord(target).then(
      (name) => {
        if (active) {
          setRecord({ target: key, name });
          setError(null);
        }
      },
      (reason: unknown) => {
        if (active) setError(displayableError(reason));
      }
    );
    return () => {
      active = false;
    };
  }, [key, refresh, target]);

  return (
    <Stack gap="xs" component="section" aria-label="Environment identity">
      <Title order={3} size="h5">
        Identity
      </Title>
      <Text size="sm">
        Current:{" "}
        {sandbox.identityName ? (
          <Anchor href={`/#/identities/${encodeURIComponent(sandbox.identityName)}`}>{sandbox.identityName}</Anchor>
        ) : (
          "none"
        )}
      </Text>
      {stopped && (
        <Stack gap="xs">
          <Select
            label="Identity for next start"
            description="Choose none to clear the identity."
            data={
              identities.identities?.flatMap((identity) => (identity.identity?.name ? [identity.identity.name] : [])) ??
              []
            }
            value={selection}
            onChange={setSelection}
            clearable
            searchable
            disabled={!identities.identities || saving}
          />
          {identities.error && (
            <Alert color="red" role="alert">
              Could not load identities: {identities.error}
            </Alert>
          )}
          {selection !== (sandbox.identityName || null) && (
            <Alert color="yellow" role="status">
              The next identity can read anything earlier agents left in this workspace, including files written with
              greater privileges.
            </Alert>
          )}
          <Button
            size="sm"
            style={{ alignSelf: "flex-start" }}
            disabled={!target || !identities.identities || selection === (sandbox.identityName || null)}
            loading={saving}
            onClick={() => void save()}
          >
            Change identity
          </Button>
        </Stack>
      )}
      {saveError && (
        <Alert color="red" role="alert">
          {saveError}
        </Alert>
      )}
      <Group justify="space-between">
        <Title order={4} size="h6">
          Retained identity record
        </Title>
        <Button size="xs" variant="subtle" onClick={() => setRefresh((value) => value + 1)}>
          Refresh record
        </Button>
      </Group>
      {error ? (
        <Alert color="red" role="alert">
          Current credential record unavailable: {error}
        </Alert>
      ) : record?.target !== key ? (
        <Text size="sm" role="status">
          Loading current credential record…
        </Text>
      ) : record.name ? (
        <Text size="sm">Credential at last check: {record.name}</Text>
      ) : (
        <Text size="sm">At last check, no retained credential named an Identity.</Text>
      )}
      <Text size="xs" c="dimmed">
        This check reads the current WorkloadCredential. Earlier records are revoked when the environment stops, so this
        is not a complete history. Refresh to see a later credential.
      </Text>
    </Stack>
  );
}
