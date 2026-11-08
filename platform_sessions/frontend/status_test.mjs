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
  const replies = new Map([
    [
      "/api/v1/instances",
      response({
        instances: providers.map((provider) => ({
          ref: { instanceId: provider.id, providerId: provider.type },
          info: { instructions: provider.instructions },
        })),
      }),
    ],
  ]);
  for (const provider of providers) {
    replies.set(
      `/api/v1/instances/${provider.id}/session`,
      response({ snapshot: { state: "SESSION_STATE_VERIFIED", ownerPresentation: { message: "Connected" } } })
    );
  }
  let redirect;
  const context = {
    document: {
      body: { dataset: {} },
      getElementById: (id) => nodes[id],
      createElement: (tag) => node(tag),
    },
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
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
      assert.ok(replies.has(url), `Unexpected request: ${url}`);
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
const alphaImport = "/api/v1/instances/two:importSession";
ui.replies.set(alphaImport, response({ outcome: "IMPORT_OUTCOME_INSTALLED" }));
await submit(event);
const sent = ui.calls.findLast(({ url }) => url === alphaImport);
const sentBody = JSON.parse(sent.options.body);
assert.equal(sentBody.mediaType, "application/json");
assert.deepEqual(JSON.parse(Buffer.from(sentBody.candidate, "base64").toString()), JSON.parse(alpha));
assert.equal(ui.nodes.session.value, "");
assert.equal(ui.nodes["import-session"].disabled, false);

const beta = exportedSession("beta", { cookies: ["synthetic"] });
const betaImport = "/api/v1/instances/beta:importSession";
// One enabled instance of a provider is selected automatically.
ui.nodes.session.value = beta;
ui.nodes.session.listeners.input();
ui.replies.set(betaImport, response({ outcome: "IMPORT_OUTCOME_INSTALLED" }));
await submit(event);
assert.equal(ui.nodes.session.value, "");
assert.equal(ui.calls.findLast(({ url }) => url === betaImport).options.method, "POST");

for (const [result, message] of [
  [{ outcome: "IMPORT_OUTCOME_REJECTED" }, /session string is invalid/],
  [{ outcome: "IMPORT_OUTCOME_REAUTHENTICATION_REQUIRED" }, /fresh sign-in/],
  [{ outcome: "IMPORT_OUTCOME_CHALLENGE_REQUIRED" }, /security challenge/],
  [{ outcome: "IMPORT_OUTCOME_UNKNOWN" }, /outcome is unknown/],
  [
    { outcome: "IMPORT_OUTCOME_UNKNOWN", outcomeUnknown: { reason: "UNKNOWN_OUTCOME_REASON_INVALID_RESPONSE" } },
    /unexpected response.*outcome is unknown/,
  ],
  [
    {
      outcome: "IMPORT_OUTCOME_UNKNOWN",
      outcomeUnknown: { reason: "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR", upstreamHttpStatus: 429 },
    },
    /HTTP 429.*outcome is unknown/,
  ],
  [
    {
      outcome: "IMPORT_OUTCOME_UNKNOWN",
      outcomeUnknown: { reason: "UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR", upstreamHttpStatus: 999 },
    },
    /HTTP error.*outcome is unknown/,
  ],
  [
    { outcome: "IMPORT_OUTCOME_UNKNOWN", outcomeUnknown: { reason: "UNKNOWN_OUTCOME_REASON_TIMEOUT" } },
    /timed out.*may have completed/,
  ],
  [{ outcome: "IMPORT_OUTCOME_UPSTREAM_UNAVAILABLE" }, /Could not connect.*may have completed/],
  [
    { outcome: "IMPORT_OUTCOME_FAILURE", failure: { reason: "IMPORT_FAILURE_REASON_INVALID_RESPONSE" } },
    /unexpected response.*Check the session status/,
  ],
  [
    { outcome: "IMPORT_OUTCOME_FAILURE", failure: { reason: "IMPORT_FAILURE_REASON_STORAGE_ERROR" } },
    /session storage.*Check the session status/,
  ],
  [
    {
      outcome: "IMPORT_OUTCOME_FAILURE",
      failure: { reason: "IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR", upstreamHttpStatus: 429 },
    },
    /HTTP 429.*Check the session status/,
  ],
]) {
  ui.nodes.session.value = beta;
  ui.nodes.session.listeners.input();
  assert.equal(ui.nodes.instance.hidden, true);
  ui.replies.set(betaImport, response({ ...result, message: "provider-internal response with private data" }));
  await submit(event);
  assert.match(ui.nodes.result.textContent, message);
  assert.doesNotMatch(ui.nodes.result.textContent, /provider-internal|private data/);
  assert.equal(ui.nodes.session.value, beta);
}

ui.nodes.session.value = beta;
ui.nodes.session.listeners.input();
ui.replies.set(
  betaImport,
  response({ outcome: "IMPORT_OUTCOME_FROM_FUTURE", message: "do not display this server text" })
);
await submit(event);
assert.equal(
  ui.nodes.result.textContent,
  "The import outcome is unknown. Check the session status before trying again."
);
assert.doesNotMatch(ui.nodes.result.textContent, /do not display/);

// An uncertain response never triggers another import, including during a poll.
let complete;
ui.replies.set(
  betaImport,
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
complete(response({ outcome: "IMPORT_OUTCOME_UNKNOWN" }));
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
const firstStatus = "/api/v1/instances/one/session";
ui.replies.set(
  firstStatus,
  () =>
    new Promise((resolve) => {
      oldStatus = resolve;
    })
);
ui.timers[0]();
await settle();
ui.replies.set(
  firstStatus,
  response({ snapshot: { state: "SESSION_STATE_VERIFIED", ownerPresentation: { message: "Fresh status" } } })
);
const probe = "/api/v1/instances/one:probe";
ui.replies.set(probe, response({}));
await ui.nodes.integrations.children[0].children[4].listeners.click();
oldStatus(
  response({
    snapshot: { state: "SESSION_STATE_REAUTHENTICATION_REQUIRED", ownerPresentation: { message: "Stale status" } },
  })
);
await settle();
assert.equal(ui.nodes.integrations.children[0].children[1].textContent, "Fresh status");
assert.equal(ui.calls.findLast(({ url }) => url === probe).options.method, "POST");
ui.replies.set(probe, response({}, 401));
await ui.nodes.integrations.children[0].children[4].listeners.click();
assert.equal(ui.redirect(), "/auth/login");

// Generic protobuf outcomes and detail names are rendered by the frontend.
const generic = page([entry("generic", "example")]);
await settle();
await settle();
generic.replies.set(
  "/api/v1/instances/generic/session",
  response({
    snapshot: {
      state: "SESSION_STATE_REAUTHENTICATION_REQUIRED",
      lastProbe: { outcome: "PROBE_OUTCOME_FAILURE", failure: { reason: "PROBE_FAILURE_REASON_STORAGE_ERROR" } },
    },
  })
);
generic.timers[0]();
await settle();
await settle();
const genericCard = generic.nodes.integrations.children[0];
assert.equal(genericCard.children[1].textContent, "Session needs reauthentication");
assert.equal(genericCard.children[3].children[0].textContent, "Imported");
assert.equal(genericCard.children[3].children[7].textContent, "storage error");
generic.replies.set(
  "/api/v1/instances/generic/session",
  response({ snapshot: { state: "SESSION_STATE_UNVERIFIED", lastProbe: { outcome: 999 } } })
);
generic.timers[0]();
await settle();
await settle();
assert.equal(genericCard.children[3].children[7].textContent, "Check outcome unknown");
