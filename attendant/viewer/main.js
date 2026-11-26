import RFB from "@novnc/novnc";
import "./viewer.css";

const login = document.querySelector("#login");
const form = document.querySelector("#login-form");
const token = document.querySelector("#token");
const loginStatus = document.querySelector("#login-status");
const workspace = document.querySelector("#workspace");
const provider = document.querySelector("#provider");
const open = document.querySelector("#open");
const verify = document.querySelector("#verify");
const bind = document.querySelector("#bind");
const logout = document.querySelector("#logout");
const providerStatus = document.querySelector("#provider-status");
const viewerStatus = document.querySelector("#viewer-status");
const screenMessage = document.querySelector("#screen-message");
const display = document.querySelector("#display");
const disconnect = document.querySelector("#disconnect");
const reconnect = document.querySelector("#reconnect");

const messages = {
  invalid_token: "The owner token was rejected.",
  invalid_origin: "Open Attendant at its configured address and try again.",
  ready: "Session verified. Caller requests are paused while you view this provider.",
  sign_in_needed: "Sign in to this provider in the remote browser, then verify the session.",
  challenge_required: "Complete the provider’s browser challenge, then verify the session.",
  binding_required: "Confirm that this is the intended account, then bind it to this provider.",
  account_changed: "Sign in as the account already bound to this provider, then verify again.",
  busy: "The browser is busy. Try again in a moment.",
  denied: "This operation was denied.",
  upstream_error: "The provider could not be reached. Try again in a moment.",
};

let desktop;
let desktopOpened = false;
let working = false;

async function api(path, options) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (response.status === 204) {
    return undefined;
  }
  const value = await response.json();
  if (!response.ok) {
    throw new Error(value.detail);
  }
  return value;
}

function post(path, body) {
  if (body === undefined) {
    return api(path, { method: "POST" });
  }
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function endDesktop() {
  const connection = desktop;
  desktop = undefined;
  connection?.disconnect();
  display.hidden = true;
  disconnect.hidden = true;
  reconnect.hidden = !desktopOpened;
  screenMessage.hidden = false;
  screenMessage.textContent = desktopOpened
    ? "Reconnect to resume the remote browser."
    : "Open a provider sign-in to use the remote browser.";
  viewerStatus.textContent = "Desktop disconnected";
}

function startDesktop() {
  if (desktop !== undefined) {
    return;
  }
  const url = new URL("/owner/viewer", window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  display.hidden = false;
  let connection;
  try {
    connection = new RFB(display, url.href);
  } catch (error) {
    endDesktop();
    throw error;
  }
  desktop = connection;
  screenMessage.hidden = true;
  viewerStatus.textContent = "Connecting to desktop…";
  disconnect.hidden = false;
  reconnect.hidden = true;
  connection.scaleViewport = true;
  connection.addEventListener("connect", () => {
    if (desktop === connection) {
      viewerStatus.textContent = "Desktop connected";
    }
  });
  connection.addEventListener("disconnect", (event) => {
    if (desktop === connection) {
      desktop = undefined;
      endDesktop();
      if (!event.detail.clean) {
        viewerStatus.textContent = "Desktop connection ended";
      }
    }
  });
}

function showLogin(message = "") {
  desktopOpened = false;
  endDesktop();
  workspace.hidden = true;
  logout.hidden = true;
  login.hidden = false;
  loginStatus.textContent = message;
  token.focus();
}

async function showWorkspace() {
  const { providers } = await api("/owner/providers");
  provider.replaceChildren(...providers.map((name) => new Option(name, name)));
  providerStatus.textContent = providers.length === 0 ? "No providers configured." : "";
  bind.hidden = true;
  open.disabled = providers.length === 0;
  verify.disabled = providers.length === 0;
  login.hidden = true;
  workspace.hidden = false;
  logout.hidden = false;
}

function showState(state) {
  providerStatus.textContent = messages[state] ?? "Session state could not be determined.";
  bind.hidden = state !== "binding_required";
}

async function providerAction(action) {
  if (working || !provider.value) {
    return;
  }
  working = true;
  const controls = [provider, open, verify, bind, logout];
  controls.forEach((control) => (control.disabled = true));
  providerStatus.textContent = "";
  try {
    await action(`/owner/providers/${encodeURIComponent(provider.value)}`);
  } catch (error) {
    if (error.message === "owner_sign_in_required") {
      showLogin("Sign in again to continue.");
    } else {
      showState(error.message);
    }
  } finally {
    working = false;
    controls.forEach((control) => (control.disabled = false));
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = form.querySelector("button");
  submit.disabled = true;
  loginStatus.textContent = "";
  try {
    await post("/owner/login", { token: token.value });
    await showWorkspace();
  } catch (error) {
    showLogin(messages[error.message] ?? "Sign-in is unavailable. Try again in a moment.");
  } finally {
    token.value = "";
    submit.disabled = false;
  }
});

logout.addEventListener("click", async () => {
  logout.disabled = true;
  try {
    await post("/owner/logout");
    showLogin();
  } catch (error) {
    if (error.message === "owner_sign_in_required") {
      showLogin();
    } else {
      providerStatus.textContent = messages[error.message] ?? "Sign-out failed. Try again.";
    }
  } finally {
    logout.disabled = false;
  }
});

open.addEventListener("click", () =>
  providerAction(async (path) => {
    bind.hidden = true;
    await post(`${path}/open`);
    desktopOpened = true;
    startDesktop();
  })
);

for (const [control, bindAccount] of [
  [verify, false],
  [bind, true],
]) {
  control.addEventListener("click", () =>
    providerAction(async (path) => {
      providerStatus.textContent = bindAccount ? "Binding account…" : "Verifying session…";
      const { state } = await post(`${path}/verify`, { bind: bindAccount });
      showState(state);
    })
  );
}

provider.addEventListener("change", () => {
  providerStatus.textContent = "";
  bind.hidden = true;
});
disconnect.addEventListener("click", endDesktop);
reconnect.addEventListener("click", async () => {
  reconnect.disabled = true;
  try {
    await api("/owner/providers");
    startDesktop();
  } catch (error) {
    if (error.message === "owner_sign_in_required") {
      showLogin("Sign in again to continue.");
    } else {
      viewerStatus.textContent = "Desktop connection failed. Try again in a moment.";
    }
  } finally {
    reconnect.disabled = false;
  }
});

showWorkspace().catch((error) => {
  showLogin(error.message === "owner_sign_in_required" ? "" : "Attendant is unavailable. Try again in a moment.");
});
