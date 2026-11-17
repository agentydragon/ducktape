import {
  Accordion,
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Container,
  Flex,
  Group,
  MantineProvider,
  NativeSelect,
  Stack,
  Table,
  Text,
  Textarea,
  Title,
} from "@mantine/core";
import "@mantine/core/styles.css";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";

type JsonObject = Record<string, unknown>;
type Instruction = { text: string; command?: string };
type Integration = { id: string; type: string; instructions: Instruction[] };
type SessionExport = { provider: string; session: JsonObject };
type Detail = { key: string; value: unknown; format?: "time" | "duration" };
type SessionStatus = { state: string; message: string; help?: string; details: Detail[] };
type ImportResult = { kind: "success" | "error" | "pending"; message: string };
type ManualProbe = { message: string; outcome: string; completedAt?: string };
type TrackedImport = { instance: string; id: string };
const importKeyPrefix = "platform-sessions-import:";

function trackedImports(): TrackedImport[] {
  const tracked: TrackedImport[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith(importKeyPrefix)) continue;
      const id = key.slice(importKeyPrefix.length);
      const instance = localStorage.getItem(key);
      if (instance && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id))
        tracked.push({ id, instance });
    }
  } catch {
    // Browsers can deny even reading localStorage; leave the dashboard usable.
  }
  return tracked;
}

const integrationRefreshIntervalMs = 30_000;

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
      Object.keys(candidate).length !== 2 ||
      typeof candidate.provider !== "string" ||
      !session ||
      typeof session !== "object" ||
      Array.isArray(session)
    )
      return null;
    return { provider: candidate.provider, session: session as JsonObject };
  } catch {
    return null;
  }
}

function endpoint(id: string): string {
  return `/api/instances/${encodeURIComponent(id)}`;
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
  outcome_unknown: "The import outcome is unknown.",
  outcome_unknown_invalid_response: "The provider returned an unexpected response. The import outcome is unknown.",
  outcome_unknown_http: "The provider returned an HTTP error. The import outcome is unknown.",
  verification_timeout: "Verification timed out. The import may have completed.",
  upstream_unavailable: "Could not connect to the provider. The import may have completed.",
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
  };
  const [state, fallback] = snapshot.storageError
    ? ["storage_error", "Could not read session state"]
    : (states[String(object(snapshot.known).state)] ?? ["unavailable", "Status unavailable"]);
  const probe = object(snapshot.lastProbe);
  const failure = object(probe.failure);
  const probeOutcome = [
    "verified",
    "reauthenticationRequired",
    "challengeRequired",
    "upstreamUnavailable",
    "outcomeUnknown",
    "noSession",
    "failure",
  ].find((name) => Object.prototype.hasOwnProperty.call(probe, name));
  const probeMessageText =
    probeOutcome === "failure" ? String(failure.reason ?? "PROBE_FAILURE_REASON_UNSPECIFIED") : probeOutcome;
  const probeMessage =
    typeof probeMessageText === "string"
      ? probeMessageText
          .replace(/^PROBE_FAILURE_REASON_/, "")
          .replace(/([a-z])([A-Z])/g, "$1 $2")
          .replaceAll("_", " ")
          .toLowerCase()
      : snapshot.lastProbe == null
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
  if (Object.prototype.hasOwnProperty.call(data, "rejected")) return importErrorMessages.invalid_session;
  if (Object.prototype.hasOwnProperty.call(data, "reauthenticationRequired"))
    return importErrorMessages.reauth_required;
  if (Object.prototype.hasOwnProperty.call(data, "challengeRequired")) return importErrorMessages.security_challenge;
  if (Object.prototype.hasOwnProperty.call(data, "upstreamUnavailable"))
    return importErrorMessages.upstream_unavailable;
  const failure = object(data.failure);
  if (failure.reason === "IMPORT_FAILURE_REASON_INVALID_RESPONSE") return importErrorMessages.invalid_response;
  if (failure.reason === "IMPORT_FAILURE_REASON_STORAGE_ERROR") return importErrorMessages.storage_error;
  if (failure.reason === "IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR") {
    const status = failure.upstreamHttpStatus;
    return Number.isInteger(status) && Number(status) >= 100 && Number(status) <= 599
      ? `The provider returned HTTP ${status}. Check the session status before retrying.`
      : importErrorMessages.upstream_unavailable;
  }
  const unknown = object(data.outcomeUnknown);
  if (unknown.reason === "UNKNOWN_OUTCOME_REASON_TIMEOUT") return importErrorMessages.verification_timeout;
  if (unknown.reason === "UNKNOWN_OUTCOME_REASON_UPSTREAM_UNREACHABLE") return importErrorMessages.upstream_unavailable;
  if (unknown.reason === "UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE")
    return importErrorMessages.outcome_unknown_invalid_response;
  if (unknown.reason === "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR") {
    const status = unknown.upstreamHttpStatus;
    return Number.isInteger(status) && Number(status) >= 100 && Number(status) <= 599
      ? `The provider returned HTTP ${status}. The import outcome is unknown.`
      : importErrorMessages.outcome_unknown_http;
  }
  return importErrorMessages.outcome_unknown;
}

function protobufCandidate(payload: SessionExport): { mediaType: string; candidate: string } {
  let binary = "";
  for (const byte of new TextEncoder().encode(JSON.stringify(payload))) binary += String.fromCharCode(byte);
  return { mediaType: "application/json", candidate: btoa(binary) };
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
  manualProbe,
  onVerify,
}: {
  info: Integration;
  status: SessionStatus | undefined;
  checking: boolean;
  importing: boolean;
  manualProbe: ManualProbe | undefined;
  onVerify: (integration: Integration) => Promise<void>;
}) {
  const current = status ?? { state: "checking", message: "Checking session…", details: [] };
  const recordedAt = current.details.find((detail) => detail.key === "checked_at")?.value;
  const showManualProbe =
    manualProbe &&
    (manualProbe.outcome === "unknown" ||
      !manualProbe.completedAt ||
      typeof recordedAt !== "string" ||
      Date.parse(manualProbe.completedAt) >= Date.parse(recordedAt));
  const manualBadges: Record<string, [string, string]> = {
    verified: ["ready", current.state === "ready" ? current.message : "Session verified"],
    "reauthentication required": ["reauth_required", "Session needs reauthentication"],
    "challenge required": ["challenge", "Provider security challenge"],
    "upstream unavailable": ["unavailable", "Provider unavailable"],
    unknown: ["outcome_unknown", "Check outcome unknown"],
    "no session": ["uninitialized", "No session configured"],
    failure: ["unavailable", "Check failed"],
  };
  const [badgeState, badgeMessage] = (showManualProbe && manualBadges[manualProbe.outcome]) || [
    current.state,
    current.message,
  ];
  return (
    <Card
      component="section"
      withBorder
      padding="lg"
      radius="md"
      flex="1 1 24rem"
      miw={0}
      aria-label={`${info.id} integration`}
    >
      <Stack gap="md">
        <Group justify="space-between" align="flex-start">
          <Title order={3} style={{ overflowWrap: "anywhere" }}>
            {info.id}
          </Title>
          <Badge
            color={stateColor(badgeState)}
            variant="light"
            h="auto"
            py={4}
            styles={{ label: { whiteSpace: "normal", overflowWrap: "anywhere" } }}
            aria-live="polite"
          >
            {badgeMessage}
          </Badge>
        </Group>
        {current.help ? (
          <Text c="dimmed" size="sm">
            {current.help}
          </Text>
        ) : null}
        {showManualProbe ? <Text aria-live="polite">{manualProbe.message}</Text> : null}
        {current.details.length > 0 ? (
          <Table aria-label={`${info.id} session details`}>
            <Table.Tbody>
              {current.details.map((detail) => (
                <Table.Tr key={detail.key}>
                  <Table.Th scope="row">{detailLabels[detail.key] ?? detail.key}</Table.Th>
                  <Table.Td>
                    {showManualProbe && detail.key === "outcome"
                      ? manualProbe.outcome
                      : showManualProbe && detail.key === "checked_at"
                        ? formatDetail({ ...detail, value: manualProbe.completedAt })
                        : showManualProbe && detail.key === "age_seconds"
                          ? "Not available"
                          : detail.key === "outcome" && typeof detail.value === "string"
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
                <Text c="dimmed">No export instructions are available.</Text>
              ) : (
                <Stack gap="sm">
                  {info.instructions.map((instruction, index) => (
                    <div key={`${index}-${instruction.text}`}>
                      <Text>{instruction.text}</Text>
                      {instruction.command ? <Code block>{instruction.command}</Code> : null}
                    </div>
                  ))}
                </Stack>
              )}
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      </Stack>
    </Card>
  );
}

function App() {
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [statuses, setStatuses] = useState<Record<string, SessionStatus>>({});
  const [checking, setChecking] = useState<Record<string, boolean>>({});
  const [probeMessages, setProbeMessages] = useState<Record<string, ManualProbe>>({});
  const [integrationError, setIntegrationError] = useState<string | null>(null);
  const [sessionText, setSessionText] = useState("");
  const [selectedInstance, setSelectedInstance] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [tracked, setTracked] = useState<TrackedImport[]>(() => {
    try {
      return trackedImports();
    } catch {
      return [];
    }
  });
  const [operationMessages, setOperationMessages] = useState<Record<string, string>>({});
  const importingRef = useRef(false);
  const integrationRefreshSequence = useRef(0);
  const foregroundImport = useRef<string | null>(null);
  const refreshSequence = useRef(new Map<string, number>());

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

  const refreshIntegrations = useCallback(async () => {
    const sequence = ++integrationRefreshSequence.current;
    const response = await fetch("/api/instances", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (signedOut(response)) return null;
    if (!response.ok) throw new Error("Integrations unavailable");
    const data: unknown = await response.json();
    const entries = integrationsFrom(data);
    if (sequence !== integrationRefreshSequence.current || importingRef.current) return null;
    setIntegrations(entries);
    setIntegrationError(null);
    return entries;
  }, []);

  const acceptOperation = useCallback((trackedImport: TrackedImport, value: unknown) => {
    const operation = object(value);
    if (operation.operationId !== trackedImport.id) throw new Error("Invalid operation");
    const has = (value: JsonObject, field: string) => Object.prototype.hasOwnProperty.call(value, field);
    if (["pending", "completed", "unknown"].filter((field) => has(operation, field)).length !== 1)
      throw new Error("Invalid operation state");
    if (has(operation, "pending")) {
      setOperationMessages((current) => ({ ...current, [trackedImport.id]: "Import in progress." }));
      return;
    }
    if (has(operation, "unknown")) {
      if (foregroundImport.current === trackedImport.id)
        setImportResult({
          kind: "error",
          message: "The import outcome is unknown. Use a fresh export before trying again.",
        });
      setOperationMessages((current) => ({
        ...current,
        [trackedImport.id]: "The import outcome is unknown. Use a fresh export before trying again.",
      }));
      return;
    }
    const result = object(operation.completed);
    const knownOutcomes = [
      "installed",
      "rejected",
      "reauthenticationRequired",
      "challengeRequired",
      "upstreamUnavailable",
      "failure",
    ];
    if (knownOutcomes.filter((field) => has(result, field)).length !== 1 || has(result, "outcomeUnknown")) {
      if (foregroundImport.current === trackedImport.id)
        setImportResult({
          kind: "error",
          message: `${protobufImportMessage(result)} Use a fresh export before trying again.`,
        });
      setOperationMessages((current) => ({
        ...current,
        [trackedImport.id]: `${protobufImportMessage(result)} Use a fresh export before trying again.`,
      }));
      return;
    }
    localStorage.removeItem(importKeyPrefix + trackedImport.id);
    setTracked(trackedImports());
    if (foregroundImport.current === null || foregroundImport.current === trackedImport.id)
      setImportResult(
        has(result, "installed")
          ? { kind: "success", message: `${trackedImport.instance} session imported.` }
          : { kind: "error", message: protobufImportMessage(result) }
      );
  }, []);

  useEffect(() => {
    let active = true;
    const check = async () => {
      for (const operation of tracked) {
        try {
          const response = await fetch(`${endpoint(operation.instance)}/imports/${operation.id}`, {
            credentials: "same-origin",
            cache: "no-store",
          });
          if (signedOut(response)) return;
          if (!response.ok) throw new Error("Operation unavailable");
          const data: unknown = await response.json();
          if (active) acceptOperation(operation, data);
        } catch {
          if (active)
            setOperationMessages((current) => ({
              ...current,
              [operation.id]: "Import status unavailable. Its outcome cannot yet be confirmed.",
            }));
        }
      }
    };
    if (tracked.length) void check();
    const timer = window.setInterval(() => void check(), 3000);
    const storageChanged = () => setTracked(trackedImports());
    window.addEventListener("storage", storageChanged);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("storage", storageChanged);
    };
  }, [tracked, acceptOperation]);

  useEffect(() => {
    let current = true;
    const initialize = async () => {
      try {
        const entries = await refreshIntegrations();
        if (!current || !entries) return;
        await Promise.all(entries.map(refresh));
      } catch {
        if (current) setIntegrationError("Could not load integrations. We'll retry automatically in 30 seconds.");
      }
    };
    void initialize();
    const timer = window.setInterval(() => {
      if (importingRef.current) return;
      void refreshIntegrations()
        .then((entries) => {
          if (!current || !entries || importingRef.current) return;
          return Promise.all(entries.map(refresh));
        })
        .catch(() => {
          // Keep the last good metadata and status visible until a later refresh succeeds.
        });
    }, integrationRefreshIntervalMs);
    return () => {
      current = false;
      window.clearInterval(timer);
      integrationRefreshSequence.current++;
    };
  }, [refresh, refreshIntegrations]);

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
      const result = object(object(await response.json()).result);
      const messages: Record<string, string> = {
        verified: "Last check: verified.",
        reauthenticationRequired: "Last check: sign in again.",
        challengeRequired: "Last check: security challenge required.",
        upstreamUnavailable: "Last check: provider unavailable.",
        outcomeUnknown: "Last check: outcome unknown; credentials may have changed.",
        noSession: "Last check: no session configured.",
        failure: "Last check: failed.",
      };
      const outcome = Object.keys(messages).find((name) => Object.prototype.hasOwnProperty.call(result, name));
      setProbeMessages((current) => ({
        ...current,
        [integration.id]: {
          message: (outcome && messages[outcome]) ?? "Last check: outcome unknown.",
          outcome:
            !outcome || outcome === "outcomeUnknown"
              ? "unknown"
              : outcome.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase(),
          completedAt: typeof result.completedAt === "string" ? result.completedAt : undefined,
        },
      }));
      await refresh(integration);
      if (outcome === "outcomeUnknown" || !outcome)
        setStatuses((current) => ({
          ...current,
          [integration.id]: {
            ...(current[integration.id] ?? { details: [] }),
            state: "outcome_unknown",
            message: "Check outcome unknown",
          },
        }));
    } catch {
      setProbeMessages((current) => ({
        ...current,
        [integration.id]: { message: "Last check: outcome unknown; credentials may have changed.", outcome: "unknown" },
      }));
      setStatuses((current) => ({
        ...current,
        [integration.id]: {
          ...(current[integration.id] ?? { state: "unavailable", details: [] }),
          state: "outcome_unknown",
          message: "Check outcome unknown",
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
    let operation: TrackedImport;
    try {
      operation = { instance: selected.id, id: crypto.randomUUID() };
      localStorage.setItem(importKeyPrefix + operation.id, operation.instance);
      setTracked(trackedImports());
    } catch {
      setImportResult({
        kind: "error",
        message: "Could not save import recovery information. The session was not sent.",
      });
      return;
    }
    importingRef.current = true;
    foregroundImport.current = operation.id;
    setImporting(true);
    setProbeMessages((current) => {
      const next = { ...current };
      delete next[selected.id];
      return next;
    });
    setSessionText("");
    setSelectedInstance(null);
    try {
      setImportResult({ kind: "pending", message: `Verifying the ${selected.id} session…` });
      const response = await fetch(`${endpoint(selected.id)}:importSession`, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...protobufCandidate(payload), operationId: operation.id }),
      });
      if (signedOut(response)) return;
      if (!response.ok) throw new Error("Import unavailable");
      acceptOperation(operation, await response.json());
    } catch {
      setImportResult({
        kind: "pending",
        message: "The import response was interrupted. Checking its recorded outcome; do not reuse the export.",
      });
    } finally {
      importingRef.current = false;
      setImporting(false);
      await refresh(selected);
    }
  };

  return (
    <MantineProvider defaultColorScheme="auto" withCssVariables={false} withGlobalClasses={false}>
      <Container size="lg" py="xl">
        <Stack gap="xl">
          <Group component="header" justify="space-between" align="flex-start">
            <div>
              <Title order={1}>Platform sessions</Title>
              <Text c="dimmed">Browser sessions for your connected services.</Text>
            </div>
            <form action="/auth/logout" method="post">
              <Button type="submit" variant="default">
                Sign out
              </Button>
            </form>
          </Group>
          <Stack component="main" gap="lg">
            {integrationError ? (
              <Alert color="red" role="status">
                {integrationError}
              </Alert>
            ) : null}
            {integrations.length > 0 ? (
              <Flex gap="md" wrap="wrap">
                {integrations.map((integration) => (
                  <IntegrationCard
                    key={integration.id}
                    info={integration}
                    status={statuses[integration.id]}
                    checking={Boolean(checking[integration.id])}
                    importing={importing}
                    manualProbe={probeMessages[integration.id]}
                    onVerify={verify}
                  />
                ))}
              </Flex>
            ) : integrationError ? null : (
              <Text c="dimmed" role="status">
                Loading integrations…
              </Text>
            )}
            <Card component="section" withBorder padding="lg" radius="md" aria-labelledby="import-heading">
              <Stack gap="md">
                <div>
                  <Title id="import-heading" order={2}>
                    Import a session
                  </Title>
                  <Text c="dimmed">
                    Open an integration’s export instructions above, copy its session, then paste it here.
                  </Text>
                </div>
                <form onSubmit={(event) => void submit(event)}>
                  <Stack gap="sm">
                    {tracked.map((operation) => (
                      <Alert key={operation.id} color="orange">
                        <Text>
                          {operation.instance}: {operationMessages[operation.id] ?? "Checking import outcome…"}
                        </Text>
                        <Text size="xs">Operation {operation.id}</Text>
                        <Button
                          variant="subtle"
                          size="xs"
                          onClick={() => {
                            localStorage.removeItem(importKeyPrefix + operation.id);
                            setTracked(trackedImports());
                          }}
                        >
                          Forget this result
                        </Button>
                      </Alert>
                    ))}
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
                    />
                    <Text c="dimmed" size="sm" aria-live="polite">
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
                    <Group>
                      <Button type="submit" loading={importing}>
                        Verify and import
                      </Button>
                    </Group>
                    {importResult ? (
                      <Alert
                        color={
                          importResult.kind === "success" ? "green" : importResult.kind === "pending" ? "blue" : "red"
                        }
                        role="status"
                        aria-live="polite"
                      >
                        {importResult.message}
                      </Alert>
                    ) : null}
                  </Stack>
                </form>
                <Text c="dimmed" size="sm">
                  Treat session exports like passwords. Clear the clipboard after importing.
                </Text>
              </Stack>
            </Card>
          </Stack>
        </Stack>
      </Container>
    </MantineProvider>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing root element");
createRoot(root).render(<App />);
