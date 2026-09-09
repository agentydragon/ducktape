import { Button, Code, Group, NativeSelect, Paper, Stack, Text, TextInput, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { displayableError } from "./client";
import { consentService, type ConsentDecision, type ConsentPreview, type ConsentService } from "./consent_client";

function continueAuthorization(url: string): void {
  window.location.assign(url);
}

/** Only the authenticated BFF can supply the continuation; client metadata is display-only. */
export function ConnectionConsent({
  handle,
  service = consentService,
  navigate = continueAuthorization,
}: {
  handle: string;
  service?: ConsentService;
  navigate?: (url: string) => void;
}): JSX.Element {
  const [preview, setPreview] = useState<ConsentPreview | null>(null);
  const [name, setName] = useState("");
  const [identity, setIdentity] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [completed, setCompleted] = useState<"allow" | "deny" | null>(null);
  // A lost response can only retry the exact decision. Never let a second click change its meaning.
  const [attempt, setAttempt] = useState<ConsentDecision | null>(null);

  useEffect(() => {
    let active = true;
    void service.preview(handle).then(
      (value) => {
        if (!active) return;
        setPreview(value);
        setAttempt(value.attempted_decision);
        setName(
          value.attempted_decision?.verdict === "allow"
            ? value.attempted_decision.display_name
            : (value.enrollment.client_name ?? "")
        );
        setIdentity(value.attempted_decision?.verdict === "allow" ? value.attempted_decision.identity_id : "");
      },
      (failure: unknown) => {
        if (active) setError(displayableError(failure));
      }
    );
    return () => {
      active = false;
    };
  }, [handle, service]);

  async function decide(decision: ConsentDecision): Promise<void> {
    setAttempt(decision);
    setSubmitting(true);
    setError(null);
    try {
      const result = await service.decide(handle, decision);
      setCompleted(result.verdict);
      if (result.verdict === "allow" && result.redirect_url !== null) navigate(result.redirect_url);
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setSubmitting(false);
    }
  }

  if (completed !== null) {
    return (
      <Stack>
        <Title order={2}>{completed === "deny" ? "Connection denied" : "Continue authorization"}</Title>
        <Text>
          {completed === "deny"
            ? "No access was granted. You can close this tab."
            : "Continuing to the identity provider. Access is not active until OAuth authorization finishes."}
        </Text>
      </Stack>
    );
  }

  const identities = Object.entries(preview?.identities ?? {}).filter(([, value]) => value.enabled);
  return (
    <Paper withBorder p="lg" maw={760} w="100%" mx="auto">
      <Stack>
        <Title order={2}>Connect to Agentplane Actions</Title>
        {error && (
          <Text role="alert" c="red">
            {error}
          </Text>
        )}
        {error && attempt !== null && (
          <Text size="sm">
            Retry sends the same decision. To change it, start a new authorization from your client.
          </Text>
        )}
        {!preview && !error && <Text>Loading authorization request…</Text>}
        {preview && (
          <>
            <Text>This client is requesting access to the Actions service.</Text>
            <Stack gap={4}>
              <Text fw={600}>Registered client (client-supplied name)</Text>
              <Text style={{ overflowWrap: "anywhere" }}>{preview.enrollment.client_name ?? "Unnamed client"}</Text>
              <Text size="sm">Client ID</Text>
              <Code style={{ overflowWrap: "anywhere" }}>{preview.enrollment.client_id}</Code>
              <Text size="sm">Validated OAuth redirect</Text>
              <Code style={{ overflowWrap: "anywhere" }}>{preview.enrollment.redirect_uri}</Code>
              <Text size="xs" c="dimmed">
                Expires {new Date(preview.enrollment.expires_at).toLocaleString()}
              </Text>
            </Stack>
            <TextInput
              label="Connection name"
              description="A name for this particular client connection, such as Claude on wyrm2."
              value={name}
              maxLength={200}
              onChange={(event) => setName(event.currentTarget.value)}
              disabled={attempt !== null}
              required
            />
            <NativeSelect
              label="Identity"
              description="The configured identity this connection will act as."
              data={[
                { value: "", label: "Choose an identity" },
                ...identities.map(([key]) => ({ value: key, label: key })),
              ]}
              value={identity}
              onChange={(event) => setIdentity(event.currentTarget.value)}
              disabled={attempt !== null}
              required
            />
            {identities.length === 0 && (
              <Text c="orange">No enabled identities are configured. You can deny this request.</Text>
            )}
            <Text size="sm">
              Connections using the same Identity share its Action receipts. This does not auto-approve Actions: new
              external clients use human approval. Each submitted Action records the specific client connection.
            </Text>
            <Group justify="flex-end">
              {attempt !== null ? (
                <Button loading={submitting} onClick={() => void decide(attempt)}>
                  Retry {attempt.verdict === "allow" ? "authorization" : "denial"}
                </Button>
              ) : (
                <>
                  <Button
                    color="red"
                    variant="light"
                    onClick={() => void decide({ verdict: "deny", csrf_token: preview.csrf_token })}
                  >
                    Deny
                  </Button>
                  <Button
                    disabled={!name.trim() || !identity}
                    onClick={() =>
                      void decide({
                        verdict: "allow",
                        csrf_token: preview.csrf_token,
                        display_name: name.trim(),
                        identity_id: identity,
                      })
                    }
                  >
                    Authorize
                  </Button>
                </>
              )}
            </Group>
          </>
        )}
      </Stack>
    </Paper>
  );
}
