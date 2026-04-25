// Visual-test harness for the Study Casino. Mocks the backend's GET /state
// + POST /events surface so the React component renders deterministic content
// without any network access. Renders into #root with the same entry shape
// the production app expects.

import React from "react";
import { createRoot } from "react-dom/client";
import StudyCasino from "../../src/study_casino.jsx";

// Frozen snapshot used as the response to GET /state. Numbers chosen to
// exercise both balance widgets, the recent-sessions list, and a non-zero
// completed-prize log. The 2025-02-01 timestamps line up with the frozen
// clock that visual-test-lib injects on every page load.
const FROZEN_NOW_MS = Date.parse("2025-02-01T12:00:00Z");
const HARNESS_STATE = {
  credits: 142,
  tokens: 88,
  sessions: [
    { id: "s1", subject: "Biochemistry", seconds: 3600, endedAt: FROZEN_NOW_MS - 2 * 3600 * 1000 },
    { id: "s2", subject: "Anatomy", seconds: 1500, endedAt: FROZEN_NOW_MS - 26 * 3600 * 1000 },
    { id: "s3", subject: "Pharmacology", seconds: 2400, endedAt: FROZEN_NOW_MS - 50 * 3600 * 1000 },
  ],
  activeSession: null,
  prizes: [
    { id: "p1", name: "Anime episode break", cost: 30 },
    { id: "p2", name: "Nice coffee shop trip", cost: 60 },
    { id: "p3", name: "Takeout night", cost: 120 },
    { id: "p4", name: "Nice dinner out with Rai", cost: 240 },
    { id: "p5", name: "Buy a new game", cost: 600 },
    { id: "p6", name: "Weekend getaway", cost: 1800 },
  ],
  prizeLog: [{ id: "log1", name: "Anime episode break", cost: 30, at: FROZEN_NOW_MS - 24 * 3600 * 1000 }],
};

const FROZEN_ETAG = '"harness-etag"';

function harnessResponse() {
  return new Response(JSON.stringify({ state: HARNESS_STATE, last_event_id: 5, etag: FROZEN_ETAG }), {
    status: 200,
    headers: { "Content-Type": "application/json", ETag: FROZEN_ETAG },
  });
}

// Replace global fetch so the storage layer's GET /state and POST /events both
// resolve immediately to the frozen snapshot. The component also exercises a
// "saved" toast on POST that fades after 1.4s — covered by the harness's
// CSS animation reset, so the toast stays invisible in the screenshot.
window.fetch = async (input) => {
  const url = typeof input === "string" ? input : input.url;
  if (url.endsWith("/state") || url.endsWith("/events")) {
    return harnessResponse();
  }
  return new Response(null, { status: 404 });
};

createRoot(document.getElementById("app")).render(<StudyCasino />);
