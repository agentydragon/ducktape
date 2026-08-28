import React from "react";

import { COLORS, projectDailyBonus } from "./shared.jsx";

function fmtCountdown(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// Permanently-visible economy strip under the header: streak, multiplier,
// rest days, and the daily-bonus lifecycle (countdown → unlocked → claimed).
// Everything except the countdown comparison comes straight from the
// server-derived credit_state.
export function StreakStrip({ creditState, activeSession, activeElapsed }) {
  const { remainingSec, unlocked } = projectDailyBonus(creditState, activeSession, activeElapsed);
  const bonus = creditState.daily_bonus_credits;

  let bonusSegment;
  if (creditState.daily_bonus_claimed_today) {
    bonusSegment = <span style={{ color: COLORS.gold }}>daily bonus claimed ✓</span>;
  } else if (unlocked) {
    bonusSegment = (
      <span style={{ color: COLORS.goldBright, fontWeight: 700 }}>
        daily bonus unlocked · +{bonus} {activeSession ? "on save" : "on your next session"}
      </span>
    );
  } else if (activeSession) {
    bonusSegment = (
      <span>
        daily bonus: +{bonus} in <span className="mono">{fmtCountdown(remainingSec)}</span>
      </span>
    );
  } else if (creditState.today_study_seconds > 0) {
    bonusSegment = (
      <span>
        daily bonus: +{bonus} after {Math.ceil(remainingSec / 60)} more min
      </span>
    );
  } else {
    bonusSegment = (
      <span>
        daily bonus: +{bonus} at {Math.ceil(creditState.daily_bonus_threshold_seconds / 60)} min
      </span>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        justifyContent: "center",
        alignItems: "baseline",
        flexWrap: "wrap",
        gap: 18,
        padding: "10px 24px",
        borderBottom: "1px solid rgba(212,165,72,0.15)",
        fontSize: 13,
        color: COLORS.creamDim,
        letterSpacing: "0.08em",
      }}
    >
      <span style={{ color: COLORS.goldBright, fontWeight: 700 }}>🔥 {creditState.streak_days}-day streak</span>
      <span>
        ×{(1 + creditState.streak_bonus_percent / 100).toFixed(2)} <span style={{ opacity: 0.7 }}>credit bonus</span>
      </span>
      {creditState.rest_days_available > 0 && (
        <span>
          {creditState.rest_days_available} rest day{creditState.rest_days_available > 1 ? "s" : ""} banked
        </span>
      )}
      {bonusSegment}
    </div>
  );
}
