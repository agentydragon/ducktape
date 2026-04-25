// Visual-test harness for the Study Casino. Pre-seeds the local Y.Doc
// directly via the casinoSync handle (no network) so the harness renders
// a deterministic, populated screenshot without contacting the backend.
//
// Why not mock /sync?  The new sync layer hydrates from `y-indexeddb`
// before its first network call; we don't want IDB state from a previous
// test run leaking in, so we open a unique IDB database per test by
// having the harness suppress the network call entirely.

import React from "react";
import { createRoot } from "react-dom/client";

import StudyCasino from "../../src/study_casino.jsx";
import { Y, casinoSync } from "../../src/sync.js";

const FROZEN_NOW_MS = Date.parse("2025-02-01T12:00:00Z");

// Block all fetches inside the harness — the test never wants the
// component to think it has reached the server.
window.fetch = async () =>
  new Response(JSON.stringify({ rejection: { rule: "harness", message: "no network in tests" } }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });

// Pre-populate the doc with the same seed data the visual baseline expects.
// The mutate() helper batches everything into one transaction so the
// observers fire once.
casinoSync.mutate(() => {
  casinoSync.balance.set("credits", 142);
  casinoSync.balance.set("tokens", 88);
  const sessionsSeed = [
    ["s1", "Biochemistry", 3600, FROZEN_NOW_MS - 2 * 3600 * 1000],
    ["s2", "Anatomy", 1500, FROZEN_NOW_MS - 26 * 3600 * 1000],
    ["s3", "Pharmacology", 2400, FROZEN_NOW_MS - 50 * 3600 * 1000],
  ];
  for (const [id, subject, seconds, endedAt] of sessionsSeed) {
    const sm = new Y.Map();
    casinoSync.sessions.set(id, sm);
    sm.set("subject", subject);
    sm.set("seconds", seconds);
    sm.set("ended_at_ms", endedAt);
  }
  const log = new Y.Map();
  casinoSync.prizeLog.push([log]);
  log.set("id", "log1");
  log.set("name", "Anime episode break");
  log.set("cost", 30);
  log.set("at_ms", FROZEN_NOW_MS - 24 * 3600 * 1000);
});

createRoot(document.getElementById("app")).render(<StudyCasino />);
