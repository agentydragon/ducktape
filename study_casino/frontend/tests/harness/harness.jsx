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
import { SCENARIOS } from "./scenarios.mjs";

// visual-test-lib freezes the wall clock via an init script before this
// bundle runs, so Date.now() *is* the frozen instant — reading it here keeps
// every relative-time fixture below in sync with the lib's frozen date
// without duplicating the magic timestamp.
const FROZEN_NOW_MS = Date.now();

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
    today_study_seconds: 3600,
    daily_bonus_threshold_seconds: 300,
    daily_bonus_credits: 30,
    pending_bonus_percent: 7,
  },
  changelog_unacked: [],
  sessions: [
    { id: "s1", subject: "Biochemistry", seconds: 3600, ended_at_ms: FROZEN_NOW_MS - 2 * 3600 * 1000 },
    { id: "s2", subject: "Anatomy", seconds: 1500, ended_at_ms: FROZEN_NOW_MS - 26 * 3600 * 1000 },
    { id: "s3", subject: "Pharmacology", seconds: 2400, ended_at_ms: FROZEN_NOW_MS - 50 * 3600 * 1000 },
  ],
  prizes: [
    { id: "p1", name: "Anime episode break", cost: 36 },
    { id: "p2", name: "Nice coffee shop trip", cost: 72 },
    { id: "p3", name: "Takeout night", cost: 144 },
    { id: "p4", name: "Nice dinner out with Rai", cost: 288 },
    { id: "p5", name: "Buy a new game", cost: 720 },
    { id: "p6", name: "Weekend getaway", cost: 2160 },
  ],
  prize_log: [{ id: "log1", name: "Anime episode break", cost: 36, at_ms: FROZEN_NOW_MS - 24 * 3600 * 1000 }],
};

// A 25-minute session on streak day 7: (25 + 30 bonus) × 1.07. Same shape
// use_casino's stopSession builds from SessionCompleteResult (decimal credits).
const AWARD_FIXTURE = {
  seconds: 25 * 60,
  creditsEarned: 58.85,
  dailyBonus: 32.1,
  streakDays: 7,
  streakBonusPercent: 7,
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
  {
    id: 2,
    date: "2026-07-18",
    title: "Reward prices recalibrated to real study habits",
    items: [
      "Prize costs and token balances were adjusted together from 2× to 1.2× legacy values.",
      "Your existing progress toward prizes was preserved, aside from at most one token of rounding.",
      "The Herman Miller Embody Chair is now 21,600 tokens.",
    ],
  },
];

// Standalone component scenarios render over the app's felt background with
// the shared stylesheet so classes like .deco-corners and .btn apply.
// minHeight: 100vh matters only for a scenario whose real subject is itself a
// `position: fixed` full-viewport overlay (e.g. ChangelogModal) — a fixed
// element contributes no size to its DOM ancestors, so #app still needs an
// explicit height to have a non-collapsed bounding box for its own element
// screenshot. A normal-flow child (e.g. SessionAwardToast, wrapped in its own
// #shot below) doesn't need or use this height at all.
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

// Unclaimed-bonus credit_state shared by the pre-crossing scenarios.
const UNCLAIMED_STATE = {
  streak_days: 16,
  streak_bonus_percent: 16,
  rest_days_available: 1,
  daily_bonus_claimed_today: false,
  today_study_seconds: 0,
  daily_bonus_threshold_seconds: 300,
  daily_bonus_credits: 30,
  pending_bonus_percent: 17,
};

// Seed an in-progress session in localStorage (where the production timer
// lives) so full-app scenarios render the active-session view. The harness
// clock is frozen at FROZEN_NOW_MS, so `minutesAgo` fixes the elapsed time.
function seedActiveSession(minutesAgo) {
  window.localStorage.setItem(
    "casino:active_session",
    JSON.stringify({
      subject: "Biochemistry",
      startTime: FROZEN_NOW_MS - minutesAgo * 60 * 1000,
      paused: false,
      pausedDuration: 0,
      pauseStartedAt: null,
    })
  );
}

// One entry per scenario, keyed as scenarios.mjs keys them: each seeds whatever state its scene
// needs and returns the tree to mount. A record rather than a switch so the scene set is data the
// check below can compare -- and so an unrecognised name fails instead of quietly rendering the
// main page, which a typo used to do.
const SCENES = {
  main_page: () => {
    casinoSync.state.set(BASE_STATE);
    return <StudyCasino />;
  },
  // Long streak with a banked rest day and today's bonus still unclaimed.
  streak_rest: () => {
    casinoSync.state.set({ ...BASE_STATE, credit_state: UNCLAIMED_STATE });
    return <StudyCasino />;
  },
  // 3 minutes into a session, 2:00 from the daily-bonus threshold.
  active_bonus_countdown: () => {
    seedActiveSession(3);
    casinoSync.state.set({ ...BASE_STATE, credit_state: UNCLAIMED_STATE });
    return <StudyCasino />;
  },
  // 6 minutes in — past the threshold: strip flips to "unlocked", live estimate includes the +30
  // at the post-qualification multiplier.
  active_bonus_unlocked: () => {
    seedActiveSession(6);
    casinoSync.state.set({ ...BASE_STATE, credit_state: UNCLAIMED_STATE });
    return <StudyCasino />;
  },
  // The #shot box shrink-wraps the toast (plus a little padding for its shadow), so the PNG is
  // just the toast on its felt backdrop — not however much of the viewport the test asks for.
  session_award: () => (
    <Standalone>
      <div id="shot" style={{ display: "inline-block", padding: 16 }}>
        <SessionAwardToast award={AWARD_FIXTURE} onDismiss={() => {}} />
      </div>
    </Standalone>
  ),
  changelog: () => (
    <Standalone>
      <ChangelogModal entries={CHANGELOG_FIXTURE} onAck={() => {}} />
    </Standalone>
  ),
};

// scenarios.mjs is what the sweep renders; SCENES is what this harness can build. A name in one
// and not the other is a scene never captured, or one the runner asks for and cannot get.
const declared = Object.keys(SCENARIOS);
const buildable = Object.keys(SCENES);
const missing = declared.filter((name) => !buildable.includes(name));
const unswept = buildable.filter((name) => !declared.includes(name));
if (missing.length || unswept.length) {
  throw new Error(
    `scenarios.mjs and harness scenes disagree: ${JSON.stringify({ missingFromHarness: missing, missingFromScenarios: unswept })}`
  );
}

const page = new URLSearchParams(window.location.search).get("page") || "main_page";
const scene = SCENES[page];
if (scene === undefined) throw new Error(`unknown harness scenario ${page}`);
const element = scene();
casinoSync.status.set({ kind: "ok", lastSyncedAt: FROZEN_NOW_MS });

createRoot(document.getElementById("app")).render(element);
