// Visual-test harness for the Study Casino. Pre-seeds the in-memory state
// cache directly via the `casinoSync.state` observable so the harness
// renders a deterministic, populated screenshot without contacting the
// backend.

import React from "react";
import { createRoot } from "react-dom/client";

import StudyCasino from "../../src/study_casino.jsx";
import { casinoSync } from "../../src/sync.js";

const FROZEN_NOW_MS = Date.parse("2025-02-01T12:00:00Z");

// Block all fetches inside the harness — the test never wants the
// component to think it has reached the server.
window.fetch = async () =>
  new Response(JSON.stringify({ detail: { rule: "harness", message: "no network in tests" } }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });

// Pre-populate the state cache with the same data the visual baseline expects.
casinoSync.state.set({
  balance: { credits: 142, tokens: 88 },
  sessions: [
    { id: "s1", subject: "Biochemistry", seconds: 3600, ended_at_ms: FROZEN_NOW_MS - 2 * 3600 * 1000 },
    { id: "s2", subject: "Anatomy", seconds: 1500, ended_at_ms: FROZEN_NOW_MS - 26 * 3600 * 1000 },
    { id: "s3", subject: "Pharmacology", seconds: 2400, ended_at_ms: FROZEN_NOW_MS - 50 * 3600 * 1000 },
  ],
  prizes: [
    { id: "p1", name: "Anime episode break", cost: 30 },
    { id: "p2", name: "Nice coffee shop trip", cost: 60 },
    { id: "p3", name: "Takeout night", cost: 120 },
    { id: "p4", name: "Nice dinner out with Rai", cost: 240 },
    { id: "p5", name: "Buy a new game", cost: 600 },
    { id: "p6", name: "Weekend getaway", cost: 1800 },
  ],
  prize_log: [{ id: "log1", name: "Anime episode break", cost: 30, at_ms: FROZEN_NOW_MS - 24 * 3600 * 1000 }],
});
casinoSync.status.set({ kind: "ok", lastSyncedAt: FROZEN_NOW_MS });

createRoot(document.getElementById("app")).render(<StudyCasino />);
