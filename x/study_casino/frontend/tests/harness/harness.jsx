// Visual-test harness for the Study Casino. Pre-seeds the in-memory state
// cache directly via the `casinoSync.state` observable so the harness
// renders a deterministic, populated screenshot without contacting the
// backend. The `?page=` query param (set by visual-test-lib) selects a
// scenario: full-app renders with different mocked `/state` payloads, or
// standalone components that carry transient React state in production.

import React from "react";
import { createRoot } from "react-dom/client";

import StudyCasino, { CasinoStyles } from "../../study_casino.jsx";
import { SessionAwardToast } from "../../StudyView.jsx";
import { ChangelogModal } from "../../ChangelogModal.jsx";
import { COLORS } from "../../shared.jsx";
import { casinoSync } from "../../sync.js";

const FROZEN_NOW_MS = Date.parse("2025-02-01T12:00:00Z");

// Block all fetches inside the harness — the test never wants the
// component to think it has reached the server.
window.fetch = async () =>
  new Response(JSON.stringify({ detail: { rule: "harness", message: "no network in tests" } }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });

const BASE_STATE = {
  balance: { credits_millis: 142500, tokens: 88 },
  credit_state: {
    streak_days: 7,
    streak_bonus_percent: 7,
    rest_days_available: 0,
    daily_bonus_claimed_today: true,
  },
  changelog_unacked: [],
  sessions: [
    { id: "s1", subject: "Biochemistry", seconds: 3600, ended_at_ms: FROZEN_NOW_MS - 2 * 3600 * 1000 },
    { id: "s2", subject: "Anatomy", seconds: 1500, ended_at_ms: FROZEN_NOW_MS - 26 * 3600 * 1000 },
    { id: "s3", subject: "Pharmacology", seconds: 2400, ended_at_ms: FROZEN_NOW_MS - 50 * 3600 * 1000 },
  ],
  prizes: [
    { id: "p1", name: "Anime episode break", cost: 60 },
    { id: "p2", name: "Nice coffee shop trip", cost: 120 },
    { id: "p3", name: "Takeout night", cost: 240 },
    { id: "p4", name: "Nice dinner out with Rai", cost: 480 },
    { id: "p5", name: "Buy a new game", cost: 1200 },
    { id: "p6", name: "Weekend getaway", cost: 3600 },
  ],
  prize_log: [{ id: "log1", name: "Anime episode break", cost: 60, at_ms: FROZEN_NOW_MS - 24 * 3600 * 1000 }],
};

// A 25-minute session on streak day 7: (25 + 30 bonus) × 1.07.
const AWARD_FIXTURE = {
  session_id: "s-award",
  seconds: 25 * 60,
  credits_earned_millis: 58850,
  daily_bonus_millis: 32100,
  streak_days: 7,
  streak_bonus_percent: 7,
};

const CHANGELOG_FIXTURE = [
  {
    id: 1,
    date: "2026-07-16",
    title: "Credit system v2: streaks, daily bonus, fairer accounting",
    items: [
      "Credits are now fractional — every second of studying counts.",
      "Daily streak: +1% credit bonus per consecutive day, up to +100%.",
      "Every 14 streak days banks a rest day that protects a single missed day.",
      "First 5 minutes each day award a +30 credit bonus (streak-multiplied).",
      "Prize costs and token balances doubled to match the boosted earning rates.",
    ],
  },
];

// Standalone component scenarios render over the app's felt background with
// the shared stylesheet so classes like .deco-corners and .btn apply.
function Standalone({ children }) {
  return (
    <div
      style={{
        background: `radial-gradient(ellipse at top, ${COLORS.felt} 0%, ${COLORS.feltDark} 60%, ${COLORS.feltDeep} 100%)`,
        minHeight: "100vh",
        color: COLORS.cream,
        fontFamily: "'Outfit', system-ui, sans-serif",
        padding: 48,
      }}
    >
      <CasinoStyles />
      {children}
    </div>
  );
}

const page = new URLSearchParams(window.location.search).get("page") || "main_page";

let element;
switch (page) {
  case "streak_rest":
    // Long streak with a banked rest day and today's bonus still unclaimed.
    casinoSync.state.set({
      ...BASE_STATE,
      credit_state: {
        streak_days: 16,
        streak_bonus_percent: 16,
        rest_days_available: 1,
        daily_bonus_claimed_today: false,
      },
    });
    element = <StudyCasino />;
    break;
  case "session_award":
    element = (
      <Standalone>
        <SessionAwardToast award={AWARD_FIXTURE} onDismiss={() => {}} />
      </Standalone>
    );
    break;
  case "changelog":
    element = (
      <Standalone>
        <ChangelogModal entries={CHANGELOG_FIXTURE} onAck={() => {}} />
      </Standalone>
    );
    break;
  default:
    casinoSync.state.set(BASE_STATE);
    element = <StudyCasino />;
}
casinoSync.status.set({ kind: "ok", lastSyncedAt: FROZEN_NOW_MS });

createRoot(document.getElementById("app")).render(element);
