import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { TextEncoder } from "node:util";
import vm from "node:vm";

const html = readFileSync(new URL("./status.html", import.meta.url), "utf8");
const source = readFileSync(new URL("./status.js", import.meta.url), "utf8");
const event = { preventDefault() {} };
const settle = () => new Promise((resolve) => setImmediate(resolve));
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

function node(tag = "div") {
  return {
    tag,
    textContent: "",
    value: "",
    disabled: false,
    hidden: false,
    dataset: {},
    listeners: {},
    children: [],
    setAttribute() {},
    addEventListener(type, callback) {
      this.listeners[type] = callback;
    },
    append(...items) {
      this.children.push(...items);
      if (this.tag === "select" && this.children.length === items.length && items.length) this.value = items[0].value;
    },
    replaceChildren(...items) {
      this.children = [];
      this.value = "";
      this.append(...items);
    },
  };
}

function page(providers) {
  const nodes = Object.fromEntries(Array.from(html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g), ([, id]) => [id, node()]));
  nodes.instance = node("select");
  const calls = [];
  const timers = [];
  const replies = new Map([["/api/integrations", response(providers)]]);
  for (const provider of providers) {
    replies.set(`/api/integrations/${provider.id}`, response({ state: "ready", message: "Connected", details: [] }));
  }
  let redirect;
  const context = {
    document: { getElementById: (id) => nodes[id], createElement: (tag) => node(tag) },
    window: {
      location: {
        assign: (url) => {
          redirect = url;
        },
      },
    },
    TextEncoder,
    setInterval: (callback) => timers.push(callback),
    fetch: async (url, options) => {
      calls.push({ url, options });
      assert.ok(replies.has(url), `Unexpected rebeta: ${url}`);
      const reply = replies.get(url);
      if (reply instanceof Error) throw reply;
      return typeof reply === "function" ? await reply() : reply;
    },
  };
  vm.runInNewContext(source, context);
  return { nodes, calls, timers, replies, redirect: () => redirect };
}

const entry = (id, type, text = "Instructions") => ({ id, type, instructions: [{ text, command: "tool chrome" }] });
const exportedSession = (provider, session) => JSON.stringify({ version: 1, provider, session });
const ui = page([entry("one", "alpha", "<unsafe>"), entry("two", "alpha"), entry("beta", "beta")]);
await settle();
await settle();
assert.equal(ui.nodes.integrations.children.length, 3);
assert.equal(ui.nodes.integrations.children[0].children[0].textContent, "one");
assert.equal(ui.nodes.integrations.children[0].children[1].textContent, "Connected");
assert.equal(ui.nodes.integrations.children[0].children[5].children[1].textContent, "<unsafe>");
assert.ok(ui.calls.every(({ options }) => options.credentials === "same-origin"));

const submit = ui.nodes["session-form"].listeners.submit;
const alpha = exportedSession("alpha", { bearer: "sess-private", organization: "org-private" });
ui.nodes.session.value = alpha;
ui.nodes.session.listeners.input();
assert.equal(ui.nodes.instance.hidden, false);
const beforeChoice = ui.calls.length;
await submit(event);
assert.equal(ui.calls.length, beforeChoice);
assert.match(ui.nodes.result.textContent, /Choose/);
ui.nodes.instance.value = "two";
ui.replies.set("/api/integrations/two/session", response({ configured: true }));
await submit(event);
const sent = ui.calls.findLast(({ url }) => url === "/api/integrations/two/session");
assert.deepEqual(JSON.parse(sent.options.body), JSON.parse(alpha));
assert.equal(ui.nodes.session.value, "");
assert.equal(ui.nodes["import-session"].disabled, false);

const beta = exportedSession("beta", { cookies: ["synthetic"] });
// One enabled instance of a provider is selected automatically.
ui.nodes.session.value = beta;
ui.nodes.session.listeners.input();
ui.replies.set("/api/integrations/beta/session", response({ state: "ready" }));
await submit(event);
assert.equal(ui.nodes.session.value, "");
assert.equal(ui.calls.findLast(({ url }) => url === "/api/integrations/beta/session").options.method, "POST");
for (const [error, status, message] of [
  ["reauth_required", 409, /fresh sign-in/],
  ["security_challenge", 503, /security challenge/],
  ["outcome_unknown", 504, /outcome is unknown/],
  ["verification_timeout", 504, /timed out.*may have completed/],
  ["upstream_unavailable", 502, /Could not connect.*may have completed/],
  ["invalid_response", 502, /unexpected response.*Check the session status/],
  ["storage_error", 503, /session storage.*Check the session status/],
  ["upstream_http_429", 502, /HTTP 429.*Check the session status/],
]) {
  ui.nodes.session.value = beta;
  ui.nodes.session.listeners.input();
  assert.equal(ui.nodes.instance.hidden, true);
  ui.replies.set(
    "/api/integrations/beta/session",
    response({ error, message: "provider-internal response with private data" }, status)
  );
  await submit(event);
  assert.match(ui.nodes.result.textContent, message);
  assert.doesNotMatch(ui.nodes.result.textContent, /provider-internal|private data/);
  assert.equal(ui.nodes.session.value, beta);
}

for (const error of ["unknown_sensitive_value", "upstream_http_999", "toString"]) {
  ui.nodes.session.value = beta;
  ui.nodes.session.listeners.input();
  ui.replies.set(
    "/api/integrations/beta/session",
    response({ error, message: "do not display this server text" }, 400)
  );
  await submit(event);
  assert.equal(ui.nodes.result.textContent, "Could not verify the session. Check the session status before retrying.");
  assert.doesNotMatch(ui.nodes.result.textContent, /unknown_sensitive|do not display/);
}

// An uncertain response never triggers another import, including during a poll.
let complete;
ui.replies.set(
  "/api/integrations/beta/session",
  () =>
    new Promise((resolve) => {
      complete = resolve;
    })
);
const pending = submit(event);
await settle();
const whileImporting = ui.calls.length;
await submit(event);
ui.timers[0]();
assert.equal(ui.calls.length, whileImporting);
complete(response({ error: "outcome_unknown" }, 504));
await pending;
assert.equal(ui.nodes["import-session"].disabled, false);

ui.nodes.session.value = exportedSession("gamma", {});
const beforeDisabled = ui.calls.length;
await submit(event);
assert.equal(ui.calls.length, beforeDisabled);
assert.match(ui.nodes.result.textContent, /not enabled/);
ui.nodes.session.value = "invalid JSON";
await submit(event);
assert.match(ui.nodes.result.textContent, /Unrecognized/);

// A slow older status response must not overwrite a later verification result.
let oldStatus;
ui.replies.set(
  "/api/integrations/one",
  () =>
    new Promise((resolve) => {
      oldStatus = resolve;
    })
);
ui.timers[0]();
await settle();
ui.replies.set("/api/integrations/one", response({ state: "ready", message: "Fresh status", details: [] }));
ui.replies.set("/api/integrations/one/verify", response({ state: "ready" }));
await ui.nodes.integrations.children[0].children[4].listeners.click();
oldStatus(response({ state: "reauth_required", message: "Stale status", details: [] }));
await settle();
assert.equal(ui.nodes.integrations.children[0].children[1].textContent, "Fresh status");
assert.equal(ui.calls.findLast(({ url }) => url === "/api/integrations/one/verify").options.method, "POST");
ui.replies.set("/api/integrations/one/verify", response({}, 401));
await ui.nodes.integrations.children[0].children[4].listeners.click();
assert.equal(ui.redirect(), "/auth/login");

// Generic outcomes and detail names are rendered by the frontend.
const generic = page([entry("generic", "example")]);
await settle();
await settle();
generic.replies.set(
  "/api/integrations/generic",
  response({
    state: "reauth_required",
    outcome: "reauth_required",
    details: [{ key: "outcome", value: "reauth_required" }],
  })
);
generic.timers[0]();
await settle();
await settle();
const genericCard = generic.nodes.integrations.children[0];
assert.equal(genericCard.children[1].textContent, "Session needs reauthentication");
assert.equal(genericCard.children[3].children[0].textContent, "Last check result");
assert.equal(genericCard.children[3].children[1].textContent, "reauth required");
