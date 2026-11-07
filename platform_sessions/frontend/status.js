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
const grpcManagement = document.body?.dataset.management === "grpc";

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
  return grpcManagement ? `/api/v1/instances/${encodeURIComponent(id)}` : `/api/integrations/${encodeURIComponent(id)}`;
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

const outcomeLabels = {
  success: "Session verified",
  uninitialized: "No session configured",
  not_checked: "Not checked yet",
  reauth_required: "Session needs reauthentication",
  upstream_http_error: "Provider could not verify the session",
  network_error: "Could not reach provider",
  invalid_response: "Provider returned an unexpected response",
  storage_error: "Could not read the session",
  superseded: "Session changed during check; check again",
};

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
  verification_timeout:
    "Verification timed out. The import may have completed; check the session status before retrying.",
  upstream_unavailable:
    "Could not connect to the provider. The import may have completed; check the session status before retrying.",
  invalid_response: "The provider returned an unexpected response. Check the session status before retrying.",
  storage_error: "Could not access session storage. Check the session status and service logs before retrying.",
};

function importErrorMessage(data, status) {
  const code = data && typeof data === "object" ? data.error : undefined;
  if (typeof code === "string") {
    if (Object.hasOwn(importErrorMessages, code)) return importErrorMessages[code];
    const upstreamStatus = /^upstream_http_([1-5]\d{2})$/.exec(code)?.[1];
    if (upstreamStatus) {
      return `The provider returned HTTP ${upstreamStatus}. Check the session status before retrying.`;
    }
  }
  return status >= 500
    ? "Import outcome is unknown; check status before retrying."
    : "Could not verify the session. Check the session status before retrying.";
}

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
  let outcome = snapshot.state === "SESSION_STATE_NO_SESSION" ? "uninitialized" : "not_checked";
  if (probe) {
    if (Object.hasOwn(probe, "verified")) outcome = "success";
    else if (Object.hasOwn(probe, "noSession")) outcome = "uninitialized";
    else if (Object.hasOwn(probe, "reauthenticationRequired")) outcome = "reauth_required";
    else if (Object.hasOwn(probe, "challengeRequired")) outcome = "security_challenge";
    else if (Object.hasOwn(probe, "upstreamUnavailable")) outcome = "network_error";
    else if (Object.hasOwn(probe, "outcomeUnknown")) outcome = "outcome_unknown";
    else if (probe.failure?.reason) outcome = probe.failure.reason.replace("PROBE_FAILURE_REASON_", "").toLowerCase();
  }
  const imported = snapshot.importedAt;
  const checked = probe?.completedAt;
  const age = imported && checked ? Math.max(0, Math.floor((Date.parse(checked) - Date.parse(imported)) / 1000)) : null;
  return {
    state,
    outcome,
    message: snapshot.ownerPresentation?.message ?? fallback,
    help: snapshot.ownerPresentation?.help,
    details: [
      { key: "imported_at", value: imported, format: "time" },
      { key: "age_seconds", value: age, format: "duration" },
      { key: "checked_at", value: checked, format: "time" },
      { key: "outcome", value: outcome },
    ],
  };
}

function protobufImportError(data) {
  if (data.rejected) return "invalid_session";
  if (data.reauthenticationRequired) return "reauth_required";
  if (data.challengeRequired) return "security_challenge";
  if (data.upstreamUnavailable) return "upstream_unavailable";
  const failure = data.failure;
  if (failure?.reason === "IMPORT_FAILURE_REASON_INVALID_RESPONSE") return "invalid_response";
  if (failure?.reason === "IMPORT_FAILURE_REASON_STORAGE_ERROR") return "storage_error";
  if (failure?.reason === "IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR" && failure.upstreamHttpStatus)
    return `upstream_http_${failure.upstreamHttpStatus}`;
  const unknown = data.outcomeUnknown;
  if (unknown?.reason === "UNKNOWN_OUTCOME_REASON_TIMEOUT") return "verification_timeout";
  if (unknown?.reason === "UNKNOWN_OUTCOME_REASON_UPSTREAM_UNREACHABLE") return "upstream_unavailable";
  if (unknown?.reason === "UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE") return "outcome_unknown_invalid_response";
  if (unknown?.reason === "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR")
    return unknown.upstreamHttpStatus ? `outcome_unknown_http_${unknown.upstreamHttpStatus}` : "outcome_unknown_http";
  return "outcome_unknown";
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
      const response = await fetch(grpcManagement ? `${endpoint(info.id)}/session` : endpoint(info.id), {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (signedOut(response)) return;
      if (!response.ok) throw new Error("Status unavailable");
      let data = await response.json();
      if (grpcManagement) data = protobufStatus(data);
      if (sequence !== refreshSequence || importing) return;
      state.textContent =
        typeof data.message === "string" ? data.message : (outcomeLabels[data.outcome] ?? "Status unavailable");
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
      const response = await fetch(grpcManagement ? `${endpoint(info.id)}:probe` : `${endpoint(info.id)}/verify`, {
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
    const response = await fetch(grpcManagement ? "/api/v1/instances" : "/api/integrations", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (signedOut(response)) return;
    if (!response.ok) throw new Error("Integrations unavailable");
    const data = await response.json();
    const entries = grpcManagement
      ? (data.instances ?? []).map((item) => ({
          id: item.ref.instanceId,
          type: item.ref.providerId,
          instructions: item.info?.instructions ?? [],
          candidate_verification: item.info?.supportsCandidateVerification ?? false,
        }))
      : data;
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
    const response = await fetch(
      grpcManagement ? `${endpoint(selected.id)}:importSession` : `${endpoint(selected.id)}/session`,
      {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(grpcManagement ? protobufCandidate(payload) : payload),
      }
    );
    if (signedOut(response)) return;
    const data = await response.json();
    if (response.ok && (!grpcManagement || Object.hasOwn(data, "installed"))) {
      sessionInput.value = "";
      updateSelection();
      importResult.textContent = `${selected.id} session imported.`;
    } else {
      importResult.textContent = importErrorMessage(
        grpcManagement && response.ok ? { error: protobufImportError(data) } : data,
        response.status
      );
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
