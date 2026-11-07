import {
  Alert,
  Button,
  Checkbox,
  Code,
  Group,
  NativeSelect,
  Paper,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import { displayableError } from "../client";
import { Verdict } from "../../../action_service/action_service_pb";
import { GrantStatus } from "../../../mcp_edge/connection_service_pb";
import { TimestampSchema, type Timestamp } from "@bufbuild/protobuf/wkt";
import { toJson } from "@bufbuild/protobuf";
import { serviceAccountKey } from "./service_accounts";
import { TimeAgo } from "../time_ago";
import { decideConsent, previewConsent, type ConsentDecision, type ConsentPreview } from "./connection_service_client";

function continueAuthorization(url: string): void {
  window.location.assign(url);
}

function timestampText(value: Timestamp | undefined): string {
  if (value === undefined) throw new Error("ConnectionService response omitted enrollment expiry");
  const jsonValue = toJson(TimestampSchema, value);
  if (typeof jsonValue !== "string") throw new Error("ConnectionService returned an invalid enrollment expiry");
  return jsonValue;
}

/** Only the authenticated BFF can supply the continuation; client metadata is display-only. */
export function ConnectionConsent({
  handle,
  navigate = continueAuthorization,
}: {
  handle: string;
  navigate?: (url: string) => void;
}): JSX.Element {
  const [preview, setPreview] = useState<ConsentPreview | null>(null);
  const [name, setName] = useState("");
  const [account, setAccount] = useState("");
  const [connectionId, setConnectionId] = useState("new");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [completed, setCompleted] = useState<"allow" | "deny" | null>(null);

  useEffect(() => {
    let active = true;
    void previewConsent(handle).then(
      (value) => {
        if (!active) return;
        setPreview(value);
        setName(value.enrollment.clientName ?? "");
      },
      (failure: unknown) => {
        if (active) setError(displayableError(failure));
      }
    );
    return () => {
      active = false;
    };
  }, [handle]);

  async function decide(decision: ConsentDecision): Promise<void> {
    setSubmitting(true);
    setError(null);
    try {
      const result = await decideConsent(handle, decision);
      const verdict = result.verdict === Verdict.ALLOW ? "allow" : "deny";
      setCompleted(verdict);
      if (verdict === "allow" && result.redirectUrl !== undefined) navigate(result.redirectUrl);
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

  const accounts = preview?.serviceAccounts ?? [];
  const selectedAccount = accounts.find((candidate) => serviceAccountKey(candidate) === account);
  const existing = preview?.connections.find((connection) => connection.id === connectionId);
  return (
    <Paper withBorder p="lg" maw={760} w="100%" mx="auto">
      <Stack>
        <Title order={2}>Connect to Agentplane Actions</Title>
        {error && (
          <Text role="alert" c="red">
            {error}
          </Text>
        )}
        {error && preview && (
          <Text size="sm">
            If the decision was recorded, deciding again is refused: start a new authorization from your MCP client.
          </Text>
        )}
        {!preview && !error && <Text>Loading authorization request…</Text>}
        {preview && (
          <>
            <Text>This client is requesting access to the Actions service.</Text>
            <Stack gap={4}>
              <Text fw={600}>Registered client (client-supplied name)</Text>
              <Text style={{ overflowWrap: "anywhere" }}>{preview.enrollment.clientName ?? "Unnamed client"}</Text>
              <Text size="sm">Client ID</Text>
              <Code style={{ overflowWrap: "anywhere" }}>{preview.enrollment.clientId}</Code>
              <Text size="sm">Validated OAuth redirect</Text>
              <Code style={{ overflowWrap: "anywhere" }}>{preview.enrollment.redirectUri}</Code>
              <Text size="xs" c="dimmed">
                Expires <TimeAgo value={timestampText(preview.enrollment.expiresAt)} />
              </Text>
            </Stack>
            <NativeSelect
              label="Connection"
              name="connection"
              data={[
                { value: "new", label: "Create a new Connection" },
                ...preview.connections.map((connection) => ({
                  value: connection.id,
                  label: `${connection.displayName} · ${connection.id}`,
                })),
              ]}
              value={connectionId}
              onChange={(event) => {
                setConnectionId(event.currentTarget.value);
                setConfirmed(false);
              }}
            />
            {connectionId === "new" ? (
              <TextInput
                label="Connection name"
                description="A name for this particular client connection, such as Claude on wyrm2."
                value={name}
                maxLength={200}
                onChange={(event) => setName(event.currentTarget.value)}
                required
              />
            ) : (
              existing && (
                <Alert
                  color="orange"
                  title={`Replace authorization for ${existing.displayName}?`}
                  data-reconnect-review
                >
                  <Stack gap="xs">
                    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                      Connection {existing.id} · reviewed version {existing.version.toString()}
                    </Text>
                    {existing.grants.map((grant) => (
                      <Text key={grant.id} size="sm" style={{ overflowWrap: "anywhere" }}>
                        Acts as {serviceAccountKey(grant.caller!)} · {grantStatusLabel(grant.status)} · client{" "}
                        {grant.clientId} · issuer {grant.issuer}
                      </Text>
                    ))}
                    <Text size="sm">
                      Fresh OAuth replaces this Connection’s authority with ServiceAccount {account || "(choose below)"}
                      . Old grants are revoked when token exchange reserves the replacement, even if issuance then
                      fails. Old tokens never switch ServiceAccount. History is preserved; already claimed work is not
                      stopped.
                    </Text>
                    <Checkbox
                      label="I confirm replacing this Connection’s authority"
                      checked={confirmed}
                      onChange={(event) => setConfirmed(event.currentTarget.checked)}
                    />
                  </Stack>
                </Alert>
              )
            )}
            <NativeSelect
              label="ServiceAccount"
              name="service_account"
              description="The labeled Kubernetes ServiceAccount this connection will act as."
              data={[
                { value: "", label: "Choose a ServiceAccount" },
                ...accounts.map((candidate) => ({
                  value: serviceAccountKey(candidate),
                  label: serviceAccountKey(candidate),
                })),
              ]}
              value={account}
              onChange={(event) => {
                setAccount(event.currentTarget.value);
                setConfirmed(false);
              }}
              required
            />
            {accounts.length === 0 && (
              <Text c="orange">No labeled caller ServiceAccounts exist. You can deny this request.</Text>
            )}
            <Text size="sm">
              Connections acting as the same ServiceAccount share its Action receipts. What it may do without an
              operator is whatever PolicyBindings name it; everything else waits for human approval. Each submitted
              Action records the specific client connection.
            </Text>
            <Group justify="flex-end">
              <Button
                color="red"
                variant="light"
                disabled={submitting}
                onClick={() => void decide({ verdict: "deny", expectedVersion: preview.enrollment.version })}
              >
                Deny
              </Button>
              <Button
                disabled={
                  submitting || !selectedAccount || (connectionId === "new" ? !name.trim() : !existing || !confirmed)
                }
                onClick={() => {
                  if (!selectedAccount) return;
                  void decide({
                    verdict: "allow",
                    expectedVersion: preview.enrollment.version,
                    connection: existing
                      ? {
                          kind: "reconnect",
                          connectionId: existing.id,
                          expectedVersion: existing.version,
                          authorityChangeConfirmed: true,
                        }
                      : { kind: "new", displayName: name.trim() },
                    serviceAccount: selectedAccount,
                  });
                }}
              >
                Authorize
              </Button>
            </Group>
          </>
        )}
      </Stack>
    </Paper>
  );
}

function grantStatusLabel(status: GrantStatus): string {
  switch (status) {
    case GrantStatus.PENDING:
      return "pending";
    case GrantStatus.ACTIVE:
      return "active";
    case GrantStatus.REVOKED:
      return "revoked";
    case GrantStatus.UNSPECIFIED:
    default:
      throw new Error(`Unknown grant status ${status}`);
  }
}
