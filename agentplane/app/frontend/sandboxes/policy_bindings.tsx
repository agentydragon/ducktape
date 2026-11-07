import { Button, Group, MultiSelect, Select, Stack, Text, Title } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import type { PolicyBinding } from "../../../policy/v1/policy_service_pb";
import type { PolicyResources } from "../policy/watch";
import { BINDING_GRANT_HELP, BindingsTable } from "../bindings/table";
import { api, displayableError, fetchWithLogin } from "../client";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import type { EnvironmentTarget } from "../../../proto/identity_pb";

/** How long a grant lasts; the Policy Controller deletes the binding when it ends. */
const GRANT_DURATIONS = [
  { value: "1", label: "1 hour" },
  { value: "4", label: "4 hours" },
  { value: "24", label: "1 day" },
  { value: "168", label: "7 days" },
  { value: "forever", label: "Until revoked" },
];

async function mutatePolicy(method: "POST" | "DELETE", path: string, body?: unknown): Promise<string | null> {
  const response = await fetchWithLogin(`/upstream/policy/v1${path}`, {
    method,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (response.ok) return null;
  let failure: unknown;
  try {
    failure = await response.json();
  } catch {
    failure = `Policy Service returned HTTP ${response.status}`;
  }
  return displayableError(failure);
}

/** Grants to a sandbox that is already running; each grant is its own binding, revoked on its own. */
function GrantPolicySets({
  policySets,
  picked,
  onPick,
  duration,
  onDuration,
  onGrant,
}: {
  policySets: string[];
  picked: string[];
  onPick: (names: string[]) => void;
  duration: string;
  onDuration: (value: string) => void;
  onGrant: () => void;
}): JSX.Element {
  return (
    <Group align="flex-end">
      <MultiSelect
        label="Policy sets"
        description="Added as a binding of its own; what this sandbox already has is untouched"
        hidePickedOptions
        data={policySets}
        value={picked}
        onChange={onPick}
        style={{ flex: "1 1 12rem" }}
      />
      <Select
        label="For"
        allowDeselect={false}
        data={GRANT_DURATIONS}
        value={duration}
        onChange={(value) => onDuration(value ?? "forever")}
        w="8rem"
      />
      <Button onClick={onGrant} disabled={picked.length === 0}>
        Grant
      </Button>
    </Group>
  );
}

/** The bindings that name the sandbox's ServiceAccount, where an operator adds one or revokes one. */
export function PolicyBindingsSection({
  target,
  resources,
}: {
  target: EnvironmentTarget;
  resources: PolicyResources | null;
}): JSX.Element {
  const [error, setError] = useState<string | null>(null);
  // The namespace's policy sets of any rule type, the ones picked to grant this sandbox next, and for how long.
  const [policySets, setPolicySets] = useState<string[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [duration, setDuration] = useState("forever");
  const [granting, setGranting] = useState(false);

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await api.GET("/sandboxes/form");
      setError(failure ? displayableError(failure) : null);
      if (!failure) setPolicySets(data.policy_sets);
    })();
  }, []);

  // What the API server now holds, granted or revoked, arrives on the page's stream; nothing to
  // re-read here.
  async function revoke(binding: PolicyBinding): Promise<void> {
    const name = binding.identity?.name;
    const uid = binding.identity?.uid;
    if (!name || !uid) {
      setError("PolicyBinding identity is unavailable; refresh the Sandbox policy view");
      return;
    }
    const path = `/sandboxes/${encodeURIComponent(target.namespace)}/${encodeURIComponent(target.name)}/policy-bindings/${encodeURIComponent(name)}`;
    const query = new URLSearchParams({ sandboxUid: target.uid, bindingUid: uid });
    try {
      setError(await mutatePolicy("DELETE", `${path}?${query}`));
    } catch (failure) {
      setError(displayableError(failure));
    }
  }

  async function grant(): Promise<void> {
    const path = `/sandboxes/${encodeURIComponent(target.namespace)}/${encodeURIComponent(target.name)}/policy-bindings`;
    const body = {
      sandboxUid: target.uid,
      policySets: picked,
      ...(duration === "forever"
        ? {}
        : { expiresAt: new Date(Date.now() + Number(duration) * 3_600_000).toISOString() }),
    };
    try {
      const failure = await mutatePolicy("POST", path, body);
      setError(failure);
      if (!failure) {
        setPicked([]);
        setGranting(false);
      }
    } catch (failure) {
      setError(displayableError(failure));
    }
  }

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Group gap={4}>
          <Title order={5}>Bindings</Title>
          <HelpTooltip topic="Bindings" description={BINDING_GRANT_HELP} />
        </Group>
        <Button
          size="compact-sm"
          variant={granting ? "light" : "default"}
          aria-expanded={granting}
          onClick={() => setGranting((open) => !open)}
        >
          Grant policy sets
        </Button>
      </Group>
      {error && <Text c="red">{error}</Text>}
      {granting && (
        <GrantPolicySets
          policySets={policySets}
          picked={picked}
          onPick={setPicked}
          duration={duration}
          onDuration={setDuration}
          onGrant={() => void grant()}
        />
      )}
      {resources && (
        <BindingsTable
          bindings={resources.bindings}
          policySets={resources.sets}
          onRevoke={(binding) => void revoke(binding)}
        />
      )}
    </Stack>
  );
}
