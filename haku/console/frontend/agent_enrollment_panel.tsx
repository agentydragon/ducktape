import { Alert, Badge, Button, Group, Loader, Radio, Stack, Text, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";

import {
  decideAgentEnrollment,
  displayableError,
  getAgentEnrollment,
  type EnrollmentDecisionRequest,
  type EnrollmentView,
} from "./client.ts";
import { toastSuccess } from "./toast.ts";

export type EnrollmentChoice = "create" | "reconnect";

export function AgentEnrollmentPanel({
  interactionId,
  initialChoice = "create",
  onReturnToSettings,
}: {
  interactionId: string;
  initialChoice?: EnrollmentChoice;
  onReturnToSettings: () => void;
}) {
  const [enrollment, setEnrollment] = useState<EnrollmentView | null>(null);
  const [choice, setChoice] = useState<EnrollmentChoice>(initialChoice);
  const [displayName, setDisplayName] = useState("");
  const [reconnectAgentId, setReconnectAgentId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState(false);

  useEffect(() => {
    let alive = true;
    void getAgentEnrollment(interactionId).then(
      (view) => {
        if (!alive) return;
        setEnrollment(view);
        setDisplayName(view.suggested_agent_name);
        setReconnectAgentId(view.reconnectable_agents[0]?.agent_id ?? null);
      },
      (reason: unknown) => {
        if (alive) setError(displayableError(reason));
      }
    );
    return () => {
      alive = false;
    };
  }, [interactionId]);

  async function decide(body: EnrollmentDecisionRequest) {
    setDeciding(true);
    setError(null);
    try {
      const result = await decideAgentEnrollment(interactionId, body);
      if (result.status === "continue") {
        window.location.assign(result.authorization_url);
        return;
      }
      toastSuccess("Agent enrollment denied", "No Agent credentials were issued.");
      onReturnToSettings();
    } catch (reason) {
      setError(displayableError(reason));
      setDeciding(false);
    }
  }

  function submit() {
    if (!enrollment) return;
    if (choice === "create") {
      void decide({ kind: "create", form_token: enrollment.form_token, display_name: displayName });
      return;
    }
    if (reconnectAgentId !== null) {
      void decide({ kind: "reconnect", form_token: enrollment.form_token, agent_id: reconnectAgentId });
    }
  }

  function deny() {
    if (enrollment) void decide({ kind: "deny", form_token: enrollment.form_token });
  }

  return (
    <section className="haku-page" aria-label="Connect an Agent">
      <header className="haku-page-header">
        <div className="haku-page-bar">
          <div>
            <Text size="xs" c="dimmed">
              Settings / Agents
            </Text>
            <Text fw={700}>Connect an Agent</Text>
          </div>
          <Button size="xs" variant="subtle" color="gray" onClick={onReturnToSettings} disabled={deciding}>
            Back
          </Button>
        </div>
      </header>
      <div className="haku-page-scroll">
        <Stack gap="md" className="haku-page-list">
          {!enrollment && !error && (
            <Group justify="center" p="xl">
              <Loader aria-label="Loading Agent enrollment" />
            </Group>
          )}
          {error && (
            <Alert color="red" title="Couldn't complete Agent enrollment">
              {error}
            </Alert>
          )}
          {enrollment && (
            <>
              <section className="haku-shell-card">
                <Stack gap="xs">
                  <Text fw={600}>{enrollment.client_software} wants to connect to Haku</Text>
                  <Text size="sm" c="dimmed">
                    Signed in as {enrollment.operator_display_name} · Redirects to {enrollment.redirect_host}
                  </Text>
                  {enrollment.requested_scopes.length > 0 && (
                    <Group gap="xs">
                      {enrollment.requested_scopes.map((scope) => (
                        <Badge key={scope} variant="light" color="gray">
                          {scope}
                        </Badge>
                      ))}
                    </Group>
                  )}
                </Stack>
              </section>

              <Radio.Group
                value={choice}
                onChange={(value) => setChoice(value as EnrollmentChoice)}
                label="What should this connection represent?"
              >
                <Stack gap="sm" mt="xs">
                  <section className="haku-shell-card">
                    <Radio value="create" label="Create a new Agent" disabled={deciding} />
                    {choice === "create" && (
                      <TextInput
                        mt="sm"
                        label="Agent name"
                        value={displayName}
                        onChange={(event) => setDisplayName(event.currentTarget.value)}
                        maxLength={80}
                        disabled={deciding}
                      />
                    )}
                  </section>

                  {enrollment.reconnectable_agents.length > 0 && (
                    <section className="haku-shell-card">
                      <Radio value="reconnect" label="Reconnect an existing Agent" disabled={deciding} />
                      {choice === "reconnect" && (
                        <Radio.Group
                          mt="sm"
                          value={reconnectAgentId ?? ""}
                          onChange={setReconnectAgentId}
                          aria-label="Existing Agent"
                        >
                          <Stack gap="xs">
                            {enrollment.reconnectable_agents.map((agent) => (
                              <Radio
                                key={agent.agent_id}
                                value={agent.agent_id}
                                label={agent.display_name}
                                disabled={deciding}
                              />
                            ))}
                          </Stack>
                        </Radio.Group>
                      )}
                    </section>
                  )}
                </Stack>
              </Radio.Group>

              <Group justify="flex-end">
                <Button variant="subtle" color="red" onClick={deny} loading={deciding}>
                  Deny request
                </Button>
                <Button
                  onClick={submit}
                  loading={deciding}
                  disabled={
                    (choice === "create" && displayName.trim().length === 0) ||
                    (choice === "reconnect" && reconnectAgentId === null)
                  }
                >
                  {choice === "create" ? "Continue" : "Reconnect"}
                </Button>
              </Group>
            </>
          )}
        </Stack>
      </div>
    </section>
  );
}
