import React from "react";

import { COLORS, fmtClock, fmtCredits, fmtHoursMin, StatCard, SUBJECTS, SectionTitle } from "./shared.jsx";

export function StudyView({
  offline,
  activeSession,
  activeElapsed,
  activeMinutes,
  todayTotal,
  sessions,
  credits,
  creditState,
  start,
  pause,
  resume,
  stop,
  cancel,
}) {
  return (
    <div>
      {/* Streak strip — retention core of credit system v2 */}
      <div
        style={{
          display: "flex",
          justifyContent: "center",
          alignItems: "baseline",
          gap: 18,
          marginBottom: 24,
          fontSize: 13,
          color: COLORS.creamDim,
          letterSpacing: "0.08em",
        }}
      >
        <span>
          <span style={{ color: COLORS.goldBright, fontWeight: 700 }}>🔥 {creditState.streak_days}-day streak</span>
        </span>
        <span>
          ×{(1 + creditState.streak_bonus_percent / 100).toFixed(2)} <span style={{ opacity: 0.7 }}>credit bonus</span>
        </span>
        {creditState.rest_days_available > 0 && (
          <span>
            {creditState.rest_days_available} rest day{creditState.rest_days_available > 1 ? "s" : ""} banked
          </span>
        )}
        <span style={{ color: creditState.daily_bonus_claimed_today ? COLORS.gold : COLORS.creamDim }}>
          {creditState.daily_bonus_claimed_today ? "daily bonus claimed ✓" : "daily bonus: +30 at 5 min"}
        </span>
      </div>
      {!activeSession ? (
        <div>
          <div
            className="display-font"
            style={{ fontSize: 32, color: COLORS.cream, textAlign: "center", marginBottom: 8 }}
          >
            Choose your subject
          </div>
          <div
            style={{
              fontSize: 13,
              color: COLORS.creamDim,
              textAlign: "center",
              marginBottom: 32,
              letterSpacing: "0.1em",
            }}
          >
            Every minute studied = 1 credit earned
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 12 }}>
            {SUBJECTS.map((s) => (
              <button
                key={s}
                onClick={() => start(s)}
                disabled={offline}
                className="deco-corners"
                style={{
                  padding: "24px 16px",
                  background: "rgba(0,0,0,0.25)",
                  border: `1px solid rgba(212,165,72,0.3)`,
                  color: offline ? COLORS.creamDim : COLORS.cream,
                  cursor: offline ? "not-allowed" : "pointer",
                  opacity: offline ? 0.35 : 1,
                  fontFamily: "'Playfair Display', serif",
                  fontSize: 16,
                  fontWeight: 600,
                  letterSpacing: "0.05em",
                  transition: "all 0.2s",
                }}
                onMouseEnter={(e) => {
                  if (offline) return;
                  e.currentTarget.style.background = "rgba(212,165,72,0.12)";
                  e.currentTarget.style.borderColor = COLORS.gold;
                }}
                onMouseLeave={(e) => {
                  if (offline) return;
                  e.currentTarget.style.background = "rgba(0,0,0,0.25)";
                  e.currentTarget.style.borderColor = "rgba(212,165,72,0.3)";
                }}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div style={{ textAlign: "center" }}>
          <div
            style={{
              fontSize: 13,
              color: COLORS.creamDim,
              letterSpacing: "0.3em",
              textTransform: "uppercase",
              marginBottom: 4,
            }}
          >
            Now studying
          </div>
          <div className="display-font" style={{ fontSize: 28, color: COLORS.gold, marginBottom: 32 }}>
            {activeSession.subject}
          </div>

          <div
            className="deco-corners"
            style={{
              display: "inline-block",
              padding: "40px 60px",
              background: "rgba(0,0,0,0.35)",
              border: `1px solid ${COLORS.gold}`,
              marginBottom: 32,
            }}
          >
            <div
              className="display-font mono"
              style={{
                fontSize: 72,
                fontWeight: 700,
                color: activeSession.paused ? COLORS.creamDim : COLORS.cream,
                lineHeight: 1,
                letterSpacing: "0.05em",
              }}
            >
              {fmtClock(activeElapsed)}
            </div>
            <div
              style={{
                marginTop: 12,
                fontSize: 13,
                color: COLORS.goldBright,
                letterSpacing: "0.2em",
                textTransform: "uppercase",
              }}
            >
              {activeSession.paused ? "Paused" : `+${activeMinutes} credits earned`}
            </div>
          </div>

          <div style={{ display: "flex", gap: 12, justifyContent: "center", flexWrap: "wrap" }}>
            {activeSession.paused ? (
              <button className="btn btn-primary" onClick={resume}>
                Resume
              </button>
            ) : (
              <button className="btn" onClick={pause}>
                Pause
              </button>
            )}
            <button className="btn btn-primary" onClick={stop} disabled={offline}>
              Stop & Save
            </button>
            <button className="btn btn-danger" onClick={cancel}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Today + recent sessions */}
      <div
        style={{ marginTop: 48, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 16 }}
      >
        <StatCard label="Studied today" value={fmtHoursMin(todayTotal)} />
        <StatCard label="Credit balance" value={fmtCredits(credits)} accent />
        <StatCard label="Total sessions" value={sessions.length.toLocaleString()} />
      </div>

      {sessions.length > 0 && (
        <div style={{ marginTop: 32 }}>
          <SectionTitle>Recent Sessions</SectionTitle>
          <div className="panel" style={{ padding: 0 }}>
            {sessions.slice(0, 8).map((s, i) => (
              <div
                key={s.id}
                style={{
                  padding: "12px 18px",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  borderBottom: i < Math.min(7, sessions.length - 1) ? `1px solid rgba(212,165,72,0.12)` : "none",
                }}
              >
                <div>
                  <div style={{ fontSize: 15, color: COLORS.cream }}>{s.subject}</div>
                  <div style={{ fontSize: 12, color: COLORS.creamDim, marginTop: 2 }}>
                    {new Date(s.endedAt).toLocaleString([], {
                      month: "short",
                      day: "numeric",
                      hour: "numeric",
                      minute: "2-digit",
                    })}
                  </div>
                </div>
                <div style={{ textAlign: "right" }}>
                  <div className="mono" style={{ fontSize: 15, color: COLORS.cream }}>
                    {fmtHoursMin(s.seconds)}
                  </div>
                  <div style={{ fontSize: 12, color: COLORS.gold, marginTop: 2 }}>+{Math.floor(s.seconds / 60)} cr</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
