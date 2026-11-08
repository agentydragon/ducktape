const container = document.getElementById("integrations");
const integrationError = document.getElementById("integrations-error");
const sessionInput = document.getElementById("session");
const instanceSelect = document.getElementById("instance");
const instanceLabel = document.getElementById("instance-label");
const importButton = document.getElementById("import-session");
const importResult = document.getElementById("result");
const detectedProvider = document.getElementById("import-provider");
let integrations = [];
let importing = false;

function signedOut(response) {
  if (response.status !== 401) return false;
  window.location.assign("/auth/login");
  return true;
}

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function endpoint(id) {
  return `/api/v1/instances/${encodeURIComponent(id)}`;
}

function identifyExport(value) {
  if (new TextEncoder().encode(value).length > 65536) return null;
  try {
    const payload = JSON.parse(value);
    if (
      !payload ||
      typeof payload !== "object" ||
      Array.isArray(payload) ||
      Object.keys(payload).length !== 3 ||
      payload.version !== 1 ||
      typeof payload.provider !== "string" ||
      !payload.session ||
      typeof payload.session !== "object" ||
      Array.isArray(payload.session)
    )
      return null;
    return payload;
  } catch {
    return null;
  }
}

function matchingInstances(payload) {
  return payload ? integrations.filter((integration) => integration.type === payload.provider) : [];
}

function updateSelection() {
  const payload = identifyExport(sessionInput.value.trim());
  const matches = matchingInstances(payload);
  instanceSelect.replaceChildren();
  instanceSelect.hidden = matches.length <= 1;
  instanceLabel.hidden = matches.length <= 1;
  if (matches.length > 1) {
    const placeholder = element("option", "Choose an integration");
    placeholder.value = "";
    instanceSelect.append(placeholder);
    for (const match of matches) {
      const option = element("option", match.id);
      option.value = match.id;
      instanceSelect.append(option);
    }
  }
  detectedProvider.textContent = !payload
    ? "Paste a JSON session export from the integration’s export instructions."
    : matches.length === 0
      ? `${payload.provider} is not enabled on this service.`
      : matches.length === 1
        ? `Detected ${matches[0].id} session`
        : `Choose which ${payload.provider} integration to replace.`;
}

function formatDetail(item) {
  if (item.value === null || item.value === undefined || item.value === "") return "Not available";
  if (item.format === "time") {
    const date = new Date(item.value);
    return Number.isNaN(date.getTime()) ? "Not available" : date.toLocaleString();
  }
  if (item.format === "duration") {
    if (!Number.isFinite(item.value) || item.value < 0) return "Not available";
    const days = Math.floor(item.value / 86400);
    const hours = Math.floor((item.value % 86400) / 3600);
    const minutes = Math.floor((item.value % 3600) / 60);
    return `${days}d ${hours}h ${minutes}m`;
  }
  return String(item.value);
}

const detailLabels = {
  imported_at: "Imported",
  age_seconds: "Age at check",
  checked_at: "Last checked",
  outcome: "Last check result",
};

const importErrorMessages = {
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
function protobufStatus(data) {
  const snapshot = data.snapshot ?? {};
  const states = {
    SESSION_STATE_NO_SESSION: ["uninitialized", "No session configured"],
    SESSION_STATE_UNVERIFIED: ["not_checked", "Session not yet verified"],
    SESSION_STATE_VERIFIED: ["ready", "Session last known to be verified"],
    SESSION_STATE_REAUTHENTICATION_REQUIRED: ["reauth_required", "Session needs reauthentication"],
    SESSION_STATE_CHALLENGE_REQUIRED: ["challenge", "Provider security challenge"],
    SESSION_STATE_OUTCOME_UNKNOWN: ["outcome_unknown", "Session outcome unknown"],
    SESSION_STATE_STORAGE_ERROR: ["storage_error", "Could not read session state"],
  };
  const [state, fallback] = states[snapshot.state] ?? ["unavailable", "Status unavailable"];
  const probe = snapshot.lastProbe;
  const probeOutcome = probe?.outcome === "PROBE_OUTCOME_FAILURE" ? probe.failure?.reason : probe?.outcome;
  const probeMessage = !probe
    ? "Not checked yet"
    : typeof probeOutcome === "string"
      ? probeOutcome
          .replace(/^PROBE_(OUTCOME|FAILURE_REASON)_/, "")
          .replaceAll("_", " ")
          .toLowerCase()
      : "Check outcome unknown";
  const imported = snapshot.importedAt;
  const checked = probe?.completedAt;
  const age = imported && checked ? Math.max(0, Math.floor((Date.parse(checked) - Date.parse(imported)) / 1000)) : null;
  return {
    state,
    message: snapshot.ownerPresentation?.message ?? fallback,
    help: snapshot.ownerPresentation?.help,
    details: [
      { key: "imported_at", value: imported, format: "time" },
      { key: "age_seconds", value: age, format: "duration" },
      { key: "checked_at", value: checked, format: "time" },
      { key: "outcome", value: probeMessage },
    ],
  };
}

function protobufImportMessage(data) {
  if (data.outcome === "IMPORT_OUTCOME_REJECTED") return importErrorMessages.invalid_session;
  if (data.outcome === "IMPORT_OUTCOME_REAUTHENTICATION_REQUIRED") return importErrorMessages.reauth_required;
  if (data.outcome === "IMPORT_OUTCOME_CHALLENGE_REQUIRED") return importErrorMessages.security_challenge;
  if (data.outcome === "IMPORT_OUTCOME_UPSTREAM_UNAVAILABLE") return importErrorMessages.upstream_unavailable;
  const failure = data.outcome === "IMPORT_OUTCOME_FAILURE" ? data.failure : null;
  if (failure?.reason === "IMPORT_FAILURE_REASON_INVALID_RESPONSE") return importErrorMessages.invalid_response;
  if (failure?.reason === "IMPORT_FAILURE_REASON_STORAGE_ERROR") return importErrorMessages.storage_error;
  if (failure?.reason === "IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR") {
    const status = failure.upstreamHttpStatus;
    return Number.isInteger(status) && status >= 100 && status <= 599
      ? `The provider returned HTTP ${status}. Check the session status before retrying.`
      : importErrorMessages.upstream_unavailable;
  }
  const unknown = data.outcome === "IMPORT_OUTCOME_UNKNOWN" ? data.outcomeUnknown : null;
  if (unknown?.reason === "UNKNOWN_OUTCOME_REASON_TIMEOUT") return importErrorMessages.verification_timeout;
  if (unknown?.reason === "UNKNOWN_OUTCOME_REASON_UPSTREAM_UNREACHABLE")
    return importErrorMessages.upstream_unavailable;
  if (unknown?.reason === "UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE")
    return importErrorMessages.outcome_unknown_invalid_response;
  if (unknown?.reason === "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR") {
    const status = unknown.upstreamHttpStatus;
    return Number.isInteger(status) && status >= 100 && status <= 599
      ? `The provider returned HTTP ${status}. The import outcome is unknown; check the session status before retrying.`
      : importErrorMessages.outcome_unknown_http;
  }
  return importErrorMessages.outcome_unknown;
}

function protobufCandidate(payload) {
  let binary = "";
  for (const byte of new TextEncoder().encode(JSON.stringify(payload))) binary += String.fromCharCode(byte);
  return { mediaType: "application/json", formatVersion: "1", candidate: btoa(binary) };
}

function setupIntegration(info) {
  const card = element("section");
  card.className = "card";
  const heading = element("h2", info.id);
  const state = element("p", "Checking session…");
  state.className = "state";
  state.setAttribute("aria-live", "polite");
  const help = element("p");
  const details = element("dl");
  const verify = element("button", "Check again");
  verify.type = "button";
  const instructions = element("details");
  instructions.append(element("summary", "Export instructions"));
  for (const step of info.instructions) {
    instructions.append(element("p", step.text));
    if (step.command) instructions.append(element("pre", step.command));
  }
  card.append(heading, state, help, details, verify, instructions);
  container.append(card);
  let refreshSequence = 0;

  async function refresh() {
    const sequence = ++refreshSequence;
    try {
      const response = await fetch(`${endpoint(info.id)}/session`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (signedOut(response)) return;
      if (!response.ok) throw new Error("Status unavailable");
      const data = protobufStatus(await response.json());
      if (sequence !== refreshSequence || importing) return;
      state.textContent = typeof data.message === "string" ? data.message : "Status unavailable";
      state.dataset.state = typeof data.state === "string" ? data.state : "unavailable";
      help.textContent = typeof data.help === "string" ? data.help : "";
      details.replaceChildren();
      for (const item of Array.isArray(data.details) ? data.details : []) {
        const label = detailLabels[item.key] ?? item.label;
        const value =
          item.key === "outcome" && typeof item.value === "string"
            ? item.value.replaceAll("_", " ")
            : formatDetail(item);
        details.append(element("dt", label), element("dd", value));
      }
    } catch {
      if (sequence !== refreshSequence || importing) return;
      state.textContent = "Status unavailable";
      state.dataset.state = "unavailable";
    }
  }

  verify.addEventListener("click", async () => {
    if (importing) return;
    verify.disabled = true;
    try {
      const response = await fetch(`${endpoint(info.id)}:probe`, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
      });
      if (signedOut(response)) return;
      if (!response.ok) throw new Error("Verification unavailable");
      await refresh();
    } catch {
      state.textContent = "Verification unavailable";
    } finally {
      verify.disabled = false;
    }
  });
  return { ...info, refresh };
}

async function initialize() {
  try {
    const response = await fetch("/api/v1/instances", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (signedOut(response)) return;
    if (!response.ok) throw new Error("Integrations unavailable");
    const data = await response.json();
    const entries = (data.instances ?? []).map((item) => ({
      id: item.ref.instanceId,
      type: item.ref.providerId,
      instructions: item.info?.instructions ?? [],
    }));
    if (!Array.isArray(entries)) throw new Error("Invalid integrations");
    integrations = entries.map(setupIntegration);
    await Promise.all(integrations.map((integration) => integration.refresh()));
    updateSelection();
  } catch {
    integrationError.textContent = "Could not load integrations. Reload the page to try again.";
  }
}

sessionInput.addEventListener("input", updateSelection);
document.getElementById("session-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (importing) return;
  const payload = identifyExport(sessionInput.value.trim());
  const matches = matchingInstances(payload);
  if (!payload) {
    importResult.textContent =
      "Unrecognized session export. Copy a fresh JSON export using the integration’s instructions.";
    return;
  }
  if (matches.length === 0) {
    importResult.textContent = `${payload.provider} is not enabled on this service. The session was not sent.`;
    return;
  }
  const selected = matches.length === 1 ? matches[0] : matches.find((item) => item.id === instanceSelect.value);
  if (!selected) {
    importResult.textContent = "Choose which integration to replace.";
    return;
  }
  importing = true;
  importButton.disabled = true;
  try {
    importResult.textContent = `Verifying the ${selected.id} session…`;
    const response = await fetch(`${endpoint(selected.id)}:importSession`, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(protobufCandidate(payload)),
    });
    if (signedOut(response)) return;
    const data = await response.json();
    if (response.ok && data.outcome === "IMPORT_OUTCOME_INSTALLED") {
      sessionInput.value = "";
      updateSelection();
      importResult.textContent = `${selected.id} session imported.`;
    } else {
      importResult.textContent = protobufImportMessage(data);
    }
  } catch {
    importResult.textContent = "The connection was interrupted. Check the session status before trying again.";
  } finally {
    importing = false;
    importButton.disabled = false;
    await selected.refresh();
  }
});

initialize();
setInterval(() => {
  if (!importing) integrations.forEach((integration) => integration.refresh());
}, 30000);
