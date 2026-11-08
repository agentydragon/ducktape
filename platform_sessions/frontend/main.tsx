import {
  Accordion,
  Alert,
  Badge,
  Button,
  Card,
  Code,
  MantineProvider,
  NativeSelect,
  Table,
  Text,
  Textarea,
  Title,
} from "@mantine/core";
import "@mantine/core/styles.css";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";

import "./app.css";

type JsonObject = Record<string, unknown>;
type Instruction = { text: string; command?: string };
type Integration = { id: string; type: string; instructions: Instruction[] };
type SessionExport = { version: 1; provider: string; session: JsonObject };
type Detail = { key: string; value: unknown; format?: "time" | "duration" };
type SessionStatus = { state: string; message: string; help?: string; details: Detail[] };
type ImportResult = { kind: "success" | "error" | "pending"; message: string };

function object(value: unknown): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function identifyExport(value: string): SessionExport | null {
  if (new TextEncoder().encode(value).length > 65536) return null;
  try {
    const payload: unknown = JSON.parse(value);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
    const candidate = payload as JsonObject;
    const session = candidate.session;
    if (
      Object.keys(candidate).length !== 3 ||
      candidate.version !== 1 ||
      typeof candidate.provider !== "string" ||
      !session ||
      typeof session !== "object" ||
      Array.isArray(session)
    )
      return null;
    return { version: 1, provider: candidate.provider, session: session as JsonObject };
  } catch {
    return null;
  }
}

function endpoint(id: string): string {
  return `/api/v1/instances/${encodeURIComponent(id)}`;
}

function formatDetail(item: Detail): string {
  if (item.value === null || item.value === undefined || item.value === "") return "Not available";
  if (item.format === "time") {
    const date = new Date(String(item.value));
    return Number.isNaN(date.getTime()) ? "Not available" : date.toLocaleString();
  }
  if (item.format === "duration") {
    if (typeof item.value !== "number" || !Number.isFinite(item.value) || item.value < 0) return "Not available";
    const days = Math.floor(item.value / 86400);
    const hours = Math.floor((item.value % 86400) / 3600);
    const minutes = Math.floor((item.value % 3600) / 60);
    return `${days}d ${hours}h ${minutes}m`;
  }
  return String(item.value);
}

const detailLabels: Record<string, string> = {
  imported_at: "Imported",
  age_seconds: "Age at check",
  checked_at: "Last checked",
  outcome: "Last check result",
};

const importErrorMessages: Record<string, string> = {
  invalid_session: "The session string is invalid. Extract a new one.",
  reauth_required: "The provider requires a fresh sign-in. Sign in and copy a fresh export.",
  security_challenge: "The provider blocked verification with a security challenge. The new session was not installed.",
  outcome_unknown: "The import outcome is unknown. Check the session status before trying again.",
  outcome_unknown_invalid_response:
    "The provider returned an unexpected response. The import outcome is unknown; check the session status before retrying.",
  outcome_unknown_http:
    "The provider returned an HTTP error. The import outcome is unknown; check the session status before retrying.",
  verification_timeout:
    "Verification timed out. The import may have completed; check the session status before retrying.",
  upstream_unavailable:
    "Could not connect to the provider. The import may have completed; check the session status before retrying.",
  invalid_response: "The provider returned an unexpected response. Check the session status before retrying.",
  storage_error: "Could not access session storage. Check the session status and service logs before retrying.",
};

// The management API returns provider protobuf messages directly. Render them here rather than
// maintaining a second server-side status schema.
function protobufStatus(data: JsonObject): SessionStatus {
  const snapshot = object(data.snapshot);
  const states: Record<string, [string, string]> = {
    SESSION_STATE_NO_SESSION: ["uninitialized", "No session configured"],
    SESSION_STATE_UNVERIFIED: ["not_checked", "Session not yet verified"],
    SESSION_STATE_VERIFIED: ["ready", "Session last known to be verified"],
    SESSION_STATE_REAUTHENTICATION_REQUIRED: ["reauth_required", "Session needs reauthentication"],
    SESSION_STATE_CHALLENGE_REQUIRED: ["challenge", "Provider security challenge"],
    SESSION_STATE_OUTCOME_UNKNOWN: ["outcome_unknown", "Session outcome unknown"],
    SESSION_STATE_STORAGE_ERROR: ["storage_error", "Could not read session state"],
  };
  const [state, fallback] = states[String(snapshot.state)] ?? ["unavailable", "Status unavailable"];
  const probe = object(snapshot.lastProbe);
  const failure = object(probe.failure);
  const probeOutcome = probe.outcome === "PROBE_OUTCOME_FAILURE" ? failure.reason : probe.outcome;
  const probeMessage =
    typeof probeOutcome === "string"
      ? probeOutcome
          .replace(/^PROBE_(OUTCOME|FAILURE_REASON)_/, "")
          .replaceAll("_", " ")
          .toLowerCase()
      : probe.outcome === undefined
        ? "Not checked yet"
        : "Check outcome unknown";
  const imported = snapshot.importedAt;
  const checked = probe.completedAt;
  const age =
    typeof imported === "string" && typeof checked === "string"
      ? Math.max(0, Math.floor((Date.parse(checked) - Date.parse(imported)) / 1000))
      : null;
  const ownerPresentation = object(snapshot.ownerPresentation);
  return {
    state,
    message: typeof ownerPresentation.message === "string" ? ownerPresentation.message : fallback,
    help: typeof ownerPresentation.help === "string" ? ownerPresentation.help : undefined,
    details: [
      { key: "imported_at", value: imported, format: "time" },
      { key: "age_seconds", value: age, format: "duration" },
      { key: "checked_at", value: checked, format: "time" },
      { key: "outcome", value: probeMessage },
    ],
  };
}

function protobufImportMessage(data: JsonObject): string {
  if (data.outcome === "IMPORT_OUTCOME_REJECTED") return importErrorMessages.invalid_session;
  if (data.outcome === "IMPORT_OUTCOME_REAUTHENTICATION_REQUIRED") return importErrorMessages.reauth_required;
  if (data.outcome === "IMPORT_OUTCOME_CHALLENGE_REQUIRED") return importErrorMessages.security_challenge;
  if (data.outcome === "IMPORT_OUTCOME_UPSTREAM_UNAVAILABLE") return importErrorMessages.upstream_unavailable;
  const failure = data.outcome === "IMPORT_OUTCOME_FAILURE" ? object(data.failure) : {};
  if (failure.reason === "IMPORT_FAILURE_REASON_INVALID_RESPONSE") return importErrorMessages.invalid_response;
  if (failure.reason === "IMPORT_FAILURE_REASON_STORAGE_ERROR") return importErrorMessages.storage_error;
  if (failure.reason === "IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR") {
    const status = failure.upstreamHttpStatus;
    return Number.isInteger(status) && Number(status) >= 100 && Number(status) <= 599
      ? `The provider returned HTTP ${status}. Check the session status before retrying.`
      : importErrorMessages.upstream_unavailable;
  }
  const unknown = data.outcome === "IMPORT_OUTCOME_UNKNOWN" ? object(data.outcomeUnknown) : {};
  if (unknown.reason === "UNKNOWN_OUTCOME_REASON_TIMEOUT") return importErrorMessages.verification_timeout;
  if (unknown.reason === "UNKNOWN_OUTCOME_REASON_UPSTREAM_UNREACHABLE") return importErrorMessages.upstream_unavailable;
  if (unknown.reason === "UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE")
    return importErrorMessages.outcome_unknown_invalid_response;
  if (unknown.reason === "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR") {
    const status = unknown.upstreamHttpStatus;
    return Number.isInteger(status) && Number(status) >= 100 && Number(status) <= 599
      ? `The provider returned HTTP ${status}. The import outcome is unknown; check the session status before retrying.`
      : importErrorMessages.outcome_unknown_http;
  }
  return importErrorMessages.outcome_unknown;
}

function protobufCandidate(payload: SessionExport): { mediaType: string; formatVersion: string; candidate: string } {
  let binary = "";
  for (const byte of new TextEncoder().encode(JSON.stringify(payload))) binary += String.fromCharCode(byte);
  return { mediaType: "application/json", formatVersion: "1", candidate: btoa(binary) };
}

function instructionsFrom(value: unknown): Instruction[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): Instruction[] => {
    const instruction = object(entry);
    if (typeof instruction.text !== "string") return [];
    return [
      {
        text: instruction.text,
        ...(typeof instruction.command === "string" ? { command: instruction.command } : {}),
      },
    ];
  });
}

function integrationsFrom(value: unknown): Integration[] {
  const data = object(value);
  if (!Array.isArray(data.instances)) throw new Error("Invalid integrations");
  return data.instances.map((entry) => {
    const item = object(entry);
    const ref = object(item.ref);
    if (typeof ref.instanceId !== "string" || typeof ref.providerId !== "string")
      throw new Error("Invalid integration");
    return {
      id: ref.instanceId,
      type: ref.providerId,
      instructions: instructionsFrom(object(item.info).instructions),
    };
  });
}

function signedOut(response: Response): boolean {
  if (response.status !== 401) return false;
  window.location.assign("/auth/login");
  return true;
}

function stateColor(state: string): string {
  if (state === "ready") return "green";
  if (state === "reauth_required" || state === "storage_error") return "red";
  if (state === "challenge" || state === "outcome_unknown") return "orange";
  if (state === "not_checked") return "yellow";
  return "gray";
}

function IntegrationCard({
  info,
  status,
  checking,
  importing,
  onVerify,
}: {
  info: Integration;
  status: SessionStatus | undefined;
  checking: boolean;
  importing: boolean;
  onVerify: (integration: Integration) => Promise<void>;
}) {
  const current = status ?? { state: "checking", message: "Checking session…", details: [] };
  return (
    <Card component="section" withBorder className="integration-card" aria-label={`${info.id} integration`}>
      <div className="integration-content">
        <div className="integration-heading">
          <Title order={3} className="integration-title">
            {info.id}
          </Title>
          <Badge
            className={`session-state session-state-${stateColor(current.state)}`}
            variant="light"
            aria-live="polite"
          >
            {current.message}
          </Badge>
        </div>
        {current.help ? <Text className="muted">{current.help}</Text> : null}
        {current.details.length > 0 ? (
          <Table className="status-details" aria-label={`${info.id} session details`}>
            <Table.Tbody>
              {current.details.map((detail) => (
                <Table.Tr key={detail.key}>
                  <Table.Th scope="row">{detailLabels[detail.key] ?? detail.key}</Table.Th>
                  <Table.Td>
                    {detail.key === "outcome" && typeof detail.value === "string"
                      ? detail.value.replaceAll("_", " ")
                      : formatDetail(detail)}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        ) : null}
        <Button variant="default" onClick={() => void onVerify(info)} disabled={importing} loading={checking}>
          Check again
        </Button>
        <Accordion variant="contained">
          <Accordion.Item value="instructions">
            <Accordion.Control>Export instructions</Accordion.Control>
            <Accordion.Panel>
              {info.instructions.length === 0 ? (
                <Text className="muted">No export instructions are available.</Text>
              ) : (
                <div className="instruction-list">
                  {info.instructions.map((instruction, index) => (
                    <div className="instruction" key={`${index}-${instruction.text}`}>
                      <Text>{instruction.text}</Text>
                      {instruction.command ? <Code block>{instruction.command}</Code> : null}
                    </div>
                  ))}
                </div>
              )}
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      </div>
    </Card>
  );
}

function App() {
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [statuses, setStatuses] = useState<Record<string, SessionStatus>>({});
  const [checking, setChecking] = useState<Record<string, boolean>>({});
  const [integrationError, setIntegrationError] = useState<string | null>(null);
  const [sessionText, setSessionText] = useState("");
  const [selectedInstance, setSelectedInstance] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const importingRef = useRef(false);
  const integrationsRef = useRef(integrations);
  const refreshSequence = useRef(new Map<string, number>());
  integrationsRef.current = integrations;

  const refresh = useCallback(async (integration: Integration) => {
    const sequence = (refreshSequence.current.get(integration.id) ?? 0) + 1;
    refreshSequence.current.set(integration.id, sequence);
    try {
      const response = await fetch(`${endpoint(integration.id)}/session`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (signedOut(response)) return;
      if (!response.ok) throw new Error("Status unavailable");
      const data: unknown = await response.json();
      if (sequence !== refreshSequence.current.get(integration.id) || importingRef.current) return;
      setStatuses((current) => ({ ...current, [integration.id]: protobufStatus(object(data)) }));
    } catch {
      if (sequence !== refreshSequence.current.get(integration.id) || importingRef.current) return;
      setStatuses((current) => ({
        ...current,
        [integration.id]: { state: "unavailable", message: "Status unavailable", details: [] },
      }));
    }
  }, []);

  useEffect(() => {
    let current = true;
    const initialize = async () => {
      try {
        const response = await fetch("/api/v1/instances", {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (signedOut(response)) return;
        if (!response.ok) throw new Error("Integrations unavailable");
        const data: unknown = await response.json();
        const entries = integrationsFrom(data);
        if (!current) return;
        setIntegrations(entries);
        await Promise.all(entries.map(refresh));
      } catch {
        if (current) setIntegrationError("Could not load integrations. Reload the page to try again.");
      }
    };
    void initialize();
    const timer = window.setInterval(() => {
      if (!importingRef.current) void Promise.all(integrationsRef.current.map(refresh));
    }, 30000);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  const identified = useMemo(() => identifyExport(sessionText.trim()), [sessionText]);
  const matches = useMemo(
    () => (identified ? integrations.filter((integration) => integration.type === identified.provider) : []),
    [identified, integrations]
  );
  const detectionMessage = !identified
    ? "Paste a JSON session export from the integration’s export instructions."
    : matches.length === 0
      ? `${identified.provider} is not enabled on this service.`
      : matches.length === 1
        ? `Detected ${matches[0].id} session`
        : `Choose which ${identified.provider} integration to replace.`;

  const verify = async (integration: Integration) => {
    if (importingRef.current) return;
    setChecking((current) => ({ ...current, [integration.id]: true }));
    try {
      const response = await fetch(`${endpoint(integration.id)}:probe`, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
      });
      if (signedOut(response)) return;
      if (!response.ok) throw new Error("Verification unavailable");
      await refresh(integration);
    } catch {
      setStatuses((current) => ({
        ...current,
        [integration.id]: {
          ...(current[integration.id] ?? { state: "unavailable", details: [] }),
          message: "Verification unavailable",
        },
      }));
    } finally {
      setChecking((current) => ({ ...current, [integration.id]: false }));
    }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (importingRef.current) return;
    const payload = identifyExport(sessionText.trim());
    const matching = payload ? integrations.filter((integration) => integration.type === payload.provider) : [];
    if (!payload) {
      setImportResult({
        kind: "error",
        message: "Unrecognized session export. Copy a fresh JSON export using the integration’s instructions.",
      });
      return;
    }
    if (matching.length === 0) {
      setImportResult({
        kind: "error",
        message: `${payload.provider} is not enabled on this service. The session was not sent.`,
      });
      return;
    }
    const selected = matching.length === 1 ? matching[0] : matching.find((item) => item.id === selectedInstance);
    if (!selected) {
      setImportResult({ kind: "error", message: "Choose which integration to replace." });
      return;
    }
    importingRef.current = true;
    setImporting(true);
    try {
      setImportResult({ kind: "pending", message: `Verifying the ${selected.id} session…` });
      const response = await fetch(`${endpoint(selected.id)}:importSession`, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(protobufCandidate(payload)),
      });
      if (signedOut(response)) return;
      const data: unknown = await response.json();
      const result = object(data);
      if (response.ok && result.outcome === "IMPORT_OUTCOME_INSTALLED") {
        setSessionText("");
        setSelectedInstance(null);
        setImportResult({ kind: "success", message: `${selected.id} session imported.` });
      } else {
        setImportResult({ kind: "error", message: protobufImportMessage(result) });
      }
    } catch {
      setImportResult({
        kind: "error",
        message: "The connection was interrupted. Check the session status before trying again.",
      });
    } finally {
      importingRef.current = false;
      setImporting(false);
      await refresh(selected);
    }
  };

  return (
    <MantineProvider defaultColorScheme="auto" withCssVariables={false} withGlobalClasses={false}>
      <header className="status-header">
        <div className="page-container status-header-content">
          <div>
            <Title order={1}>Platform sessions</Title>
            <Text className="muted">Browser sessions for your connected services.</Text>
          </div>
          <form action="/auth/logout" method="post">
            <Button type="submit" variant="default">
              Sign out
            </Button>
          </form>
        </div>
      </header>
      <main className="page-container status-page">
        <div className="page-content">
          {integrationError ? (
            <Alert className="integration-error" role="status">
              {integrationError}
            </Alert>
          ) : null}
          {integrations.length > 0 ? (
            <div className="providers">
              {integrations.map((integration) => (
                <IntegrationCard
                  key={integration.id}
                  info={integration}
                  status={statuses[integration.id]}
                  checking={Boolean(checking[integration.id])}
                  importing={importing}
                  onVerify={verify}
                />
              ))}
            </div>
          ) : integrationError ? null : (
            <Text className="muted" role="status">
              Loading integrations…
            </Text>
          )}
          <Card component="section" withBorder className="import-card" aria-labelledby="import-heading">
            <div className="import-content">
              <div>
                <Title id="import-heading" order={2}>
                  Import a session
                </Title>
                <Text className="muted">
                  Open an integration’s export instructions above, copy its session, then paste it here.
                </Text>
              </div>
              <form onSubmit={(event) => void submit(event)}>
                <div className="import-fields">
                  <Textarea
                    label="Session export"
                    value={sessionText}
                    onChange={(event) => {
                      setSessionText(event.currentTarget.value);
                      setSelectedInstance(null);
                    }}
                    required
                    rows={6}
                    autoComplete="off"
                    spellCheck={false}
                    className="session-input"
                  />
                  <Text className="muted" aria-live="polite">
                    {detectionMessage}
                  </Text>
                  {matches.length > 1 ? (
                    <NativeSelect
                      label="Choose an integration"
                      value={selectedInstance ?? ""}
                      onChange={(event) => setSelectedInstance(event.currentTarget.value || null)}
                    >
                      <option value="">Choose which session to replace</option>
                      {matches.map((integration) => (
                        <option key={integration.id} value={integration.id}>
                          {integration.id}
                        </option>
                      ))}
                    </NativeSelect>
                  ) : null}
                  <Button type="submit" loading={importing}>
                    Verify and import
                  </Button>
                  {importResult ? (
                    <Alert
                      className={`import-result import-result-${importResult.kind}`}
                      role="status"
                      aria-live="polite"
                    >
                      {importResult.message}
                    </Alert>
                  ) : null}
                </div>
              </form>
              <Text className="muted">Treat session exports like passwords. Clear the clipboard after importing.</Text>
            </div>
          </Card>
        </div>
      </main>
    </MantineProvider>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing root element");
createRoot(root).render(<App />);
