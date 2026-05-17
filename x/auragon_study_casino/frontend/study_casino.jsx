import React, { useState, useEffect, useMemo } from "react";

import { SyncIcon } from "./SyncIcon.jsx";
import { useCasino } from "./use_casino.js";
import { COLORS, SUBJECTS, fmtClock, fmtHoursMin, getElapsedSec } from "./shared.jsx";
import { StudyView } from "./StudyView.jsx";
import { PrizesView } from "./PrizesView.jsx";
import { StatsView } from "./StatsView.jsx";
import { AdminView } from "./AdminView.jsx";
import { Roulette } from "./Roulette.jsx";
import { Blackjack } from "./Blackjack.jsx";
import { Slots } from "./Slots.jsx";

export default function StudyCasino() {
  const [view, setView] = useState("study");
  const casino = useCasino();
  const {
    offline,
    username,
    isAdmin,
    credits,
    tokens,
    sessions,
    prizes,
    prizeLog,
    activeSession,
    startSession,
    pauseSession,
    resumeSession,
    stopSession,
    cancelSession,
    editSession,
    deleteSession,
    addPastSession,
    redeemPrize,
    addPrize,
    deletePrize,
    convertToTokens,
    spinSlots,
    spinRoulette,
    blackjackDeal,
    blackjackHit,
    blackjackStand,
    blackjackDouble,
    exportData,
    importData,
    resetData,
  } = casino;

  const [, setTick] = useState(0);
  useEffect(() => {
    if (!activeSession || activeSession.paused) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [activeSession?.startTime, activeSession?.paused]);

  const activeElapsed = activeSession ? getElapsedSec(activeSession) : 0;
  const activeMinutes = Math.floor(activeElapsed / 60);

  const todayTotal = useMemo(() => {
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    const t = start.getTime();
    return sessions.filter((s) => s.endedAt >= t).reduce((a, s) => a + s.seconds, 0);
  }, [sessions]);

  const totalStudied = useMemo(() => sessions.reduce((a, s) => a + s.seconds, 0), [sessions]);

  const bySubject = useMemo(() => {
    const m = {};
    SUBJECTS.forEach((s) => (m[s] = 0));
    sessions.forEach((s) => {
      m[s.subject] = (m[s.subject] || 0) + s.seconds;
    });
    return m;
  }, [sessions]);

  return (
    <div
      style={{
        background: `radial-gradient(ellipse at top, ${COLORS.felt} 0%, ${COLORS.feltDark} 60%, ${COLORS.feltDeep} 100%)`,
        minHeight: "100vh",
        color: COLORS.cream,
        fontFamily: "'Outfit', system-ui, sans-serif",
      }}
    >
      <style>{`
        /* Fonts are loaded once, hermetically, via /fonts/fonts.css linked in
           index.html — kept out of this CSS-in-JSX block so the woff2 files
           are bundled by Bazel rather than fetched at runtime from a CDN. */

        * { box-sizing: border-box; }

        .display-font { font-family: 'Playfair Display', Georgia, serif; letter-spacing: 0.02em; }
        .mono { font-variant-numeric: tabular-nums; font-feature-settings: 'tnum'; }

        .btn {
          font-family: 'Outfit', sans-serif;
          font-weight: 500;
          letter-spacing: 0.05em;
          text-transform: uppercase;
          font-size: 13px;
          padding: 10px 20px;
          border-radius: 2px;
          border: 1px solid ${COLORS.gold};
          background: transparent;
          color: ${COLORS.gold};
          cursor: pointer;
          transition: all 0.2s;
        }
        .btn:hover:not(:disabled) { background: ${COLORS.gold}; color: ${COLORS.feltDeep}; }
        .btn:disabled { opacity: 0.35; cursor: not-allowed; }
        .btn-primary {
          background: ${COLORS.gold};
          color: ${COLORS.feltDeep};
          font-weight: 600;
        }
        .btn-primary:hover:not(:disabled) { background: ${COLORS.goldBright}; border-color: ${COLORS.goldBright}; }
        .btn-danger {
          border-color: ${COLORS.red};
          color: ${COLORS.red};
        }
        .btn-danger:hover:not(:disabled) { background: ${COLORS.red}; color: ${COLORS.cream}; }

        .panel {
          background: rgba(0,0,0,0.25);
          border: 1px solid rgba(212,165,72,0.25);
          border-radius: 3px;
        }

        .gold-border { border: 1px solid ${COLORS.gold}; }
        .gold-border-dim { border: 1px solid rgba(212,165,72,0.3); }

        .nav-link {
          font-family: 'Playfair Display', serif;
          font-size: 15px;
          letter-spacing: 0.15em;
          text-transform: uppercase;
          padding: 8px 16px;
          cursor: pointer;
          color: ${COLORS.creamDim};
          border-bottom: 2px solid transparent;
          transition: all 0.2s;
          background: transparent;
          border-left: none; border-right: none; border-top: none;
        }
        .nav-link:hover { color: ${COLORS.cream}; }
        .nav-link.active { color: ${COLORS.gold}; border-bottom-color: ${COLORS.gold}; }

        input, select {
          background: rgba(0,0,0,0.3);
          border: 1px solid rgba(212,165,72,0.4);
          color: ${COLORS.cream};
          padding: 8px 12px;
          border-radius: 2px;
          font-family: 'Outfit', sans-serif;
          font-size: 14px;
        }
        input:focus, select:focus { outline: none; border-color: ${COLORS.gold}; }
        select option { background: ${COLORS.feltDark}; color: ${COLORS.cream}; }

        @keyframes spin-wheel {
          from { transform: rotate(var(--from)); }
          to   { transform: rotate(var(--to)); }
        }

        @keyframes reel-spin {
          from { transform: translateY(0); }
          to { transform: translateY(var(--to)); }
        }

        @keyframes pulse-gold {
          0%, 100% { box-shadow: 0 0 0 rgba(232,184,74,0); }
          50% { box-shadow: 0 0 20px rgba(232,184,74,0.6); }
        }

        @keyframes pulse-dot {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.4; }
        }

        /* === WIN ANIMATION === */
        @keyframes win-particle {
          0%   { transform: translate(0, 0) rotate(0deg) scale(0.4); opacity: 0; }
          10%  { opacity: 1; transform: translate(0, 0) rotate(0deg) scale(1.1); }
          70%  { opacity: 1; }
          100% {
            transform: translate(var(--dx), calc(var(--dy) + 80px)) rotate(var(--rot)) scale(0.9);
            opacity: 0;
          }
        }
        @keyframes win-text-pop {
          0%   { transform: translate(-50%, -50%) scale(0.2); opacity: 0; filter: blur(8px); }
          25%  { transform: translate(-50%, -50%) scale(1.4); opacity: 1; filter: blur(0); }
          70%  { transform: translate(-50%, -50%) scale(1.05); opacity: 1; }
          100% { transform: translate(-50%, -50%) scale(1.05); opacity: 0; }
        }
        @keyframes win-flash {
          0%   { opacity: 0; }
          15%  { opacity: 0.55; }
          100% { opacity: 0; }
        }
        @keyframes win-ring {
          0%   { transform: translate(-50%, -50%) scale(0.1); opacity: 0.9; }
          100% { transform: translate(-50%, -50%) scale(3.2); opacity: 0; }
        }

        .deco-corners { position: relative; }
        .deco-corners::before, .deco-corners::after {
          content: '';
          position: absolute; width: 14px; height: 14px;
          border: 1px solid ${COLORS.gold};
        }
        .deco-corners::before { top: -1px; left: -1px; border-right: none; border-bottom: none; }
        .deco-corners::after { bottom: -1px; right: -1px; border-left: none; border-top: none; }

      `}</style>

      {/* Header */}
      <header
        style={{
          borderBottom: `1px solid rgba(212,165,72,0.3)`,
          background: "rgba(0,0,0,0.3)",
          padding: "16px 28px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: 16,
        }}
      >
        <div>
          <div className="display-font" style={{ fontSize: 24, fontWeight: 900, color: COLORS.gold, lineHeight: 1 }}>
            ♠ AURAGON'S ♠
          </div>
          <div
            className="display-font"
            style={{ fontSize: 13, color: COLORS.creamDim, letterSpacing: "0.3em", marginTop: 2 }}
          >
            STUDY CASINO
          </div>
        </div>

        <nav style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {["study", "casino", "prizes", "stats", ...(isAdmin ? ["admin"] : [])].map((v) => (
            <button key={v} className={`nav-link ${view === v ? "active" : ""}`} onClick={() => setView(v)}>
              {v}
            </button>
          ))}
        </nav>

        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "6px 14px",
              border: `1px solid ${COLORS.wine}`,
              background: "rgba(90,31,42,0.3)",
            }}
          >
            <div style={{ fontSize: 10, color: COLORS.creamDim, letterSpacing: "0.2em", textTransform: "uppercase" }}>
              Tokens
            </div>
            <div className="display-font mono" style={{ fontSize: 20, color: COLORS.rose, fontWeight: 700 }}>
              {tokens.toLocaleString()}
            </div>
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "6px 14px",
              border: `1px solid ${COLORS.gold}`,
              background: "rgba(0,0,0,0.4)",
            }}
          >
            <div style={{ fontSize: 10, color: COLORS.creamDim, letterSpacing: "0.2em", textTransform: "uppercase" }}>
              Credits
            </div>
            <div className="display-font mono" style={{ fontSize: 20, color: COLORS.gold, fontWeight: 700 }}>
              {credits.toLocaleString()}
            </div>
          </div>
          <SyncIcon />
        </div>
      </header>

      {offline && (
        <div
          style={{
            background: "rgba(180,40,40,0.85)",
            borderBottom: `1px solid ${COLORS.red}`,
            padding: "8px 28px",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 12,
            fontSize: 13,
            color: COLORS.cream,
            letterSpacing: "0.05em",
          }}
        >
          <span style={{ fontWeight: 600 }}>Backend unreachable</span>
          <span style={{ opacity: 0.7 }}>— controls are disabled until connection is restored</span>
        </div>
      )}

      {activeSession && (
        <div
          style={{
            background: activeSession.paused ? "rgba(122,40,56,0.45)" : "rgba(31,10,16,0.92)",
            borderBottom: `1px solid ${activeSession.paused ? COLORS.wine : COLORS.gold}`,
            padding: "10px 28px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 16,
            flexWrap: "wrap",
            position: "sticky",
            top: 0,
            zIndex: 50,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <span
              style={{
                display: "inline-block",
                width: 9,
                height: 9,
                borderRadius: "50%",
                background: activeSession.paused ? "#c9bc9a" : "#65e0a0",
                boxShadow: activeSession.paused ? "none" : "0 0 8px rgba(101,224,160,0.8)",
                animation: activeSession.paused ? "none" : "pulse-dot 1.5s ease-in-out infinite",
              }}
            />
            <span className="display-font" style={{ color: COLORS.cream, fontSize: 16, fontWeight: 600 }}>
              {activeSession.subject}
            </span>
            <span
              className="mono"
              style={{
                color: activeSession.paused ? COLORS.creamDim : COLORS.gold,
                fontSize: 18,
                fontWeight: 500,
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {fmtClock(activeElapsed)}
            </span>
            <span style={{ fontSize: 11, color: COLORS.creamDim, letterSpacing: "0.15em", textTransform: "uppercase" }}>
              {activeSession.paused ? "Paused" : `+${activeMinutes} cr earned`}
            </span>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            {activeSession.paused ? (
              <button
                onClick={resumeSession}
                style={{
                  padding: "6px 14px",
                  fontSize: 12,
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  background: COLORS.gold,
                  color: COLORS.feltDeep,
                  border: "none",
                  fontWeight: 600,
                  cursor: "pointer",
                  borderRadius: 2,
                }}
              >
                Resume
              </button>
            ) : (
              <button
                onClick={pauseSession}
                style={{
                  padding: "6px 14px",
                  fontSize: 12,
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  background: "transparent",
                  color: COLORS.cream,
                  border: `1px solid ${COLORS.creamDim}`,
                  cursor: "pointer",
                  borderRadius: 2,
                }}
              >
                Pause
              </button>
            )}
            <button
              onClick={stopSession}
              disabled={offline}
              style={{
                padding: "6px 14px",
                fontSize: 12,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                background: "transparent",
                color: offline ? COLORS.creamDim : COLORS.gold,
                border: `1px solid ${COLORS.gold}`,
                cursor: offline ? "not-allowed" : "pointer",
                opacity: offline ? 0.35 : 1,
                borderRadius: 2,
              }}
            >
              Stop
            </button>
          </div>
        </div>
      )}

      <main style={{ maxWidth: 1100, margin: "0 auto", padding: "32px 24px 60px" }}>
        {view === "study" && (
          <StudyView
            offline={offline}
            activeSession={activeSession}
            activeElapsed={activeElapsed}
            activeMinutes={activeMinutes}
            todayTotal={todayTotal}
            sessions={sessions}
            credits={credits}
            start={startSession}
            pause={pauseSession}
            resume={resumeSession}
            stop={stopSession}
            cancel={cancelSession}
          />
        )}
        {view === "casino" && (
          <CasinoView
            offline={offline}
            credits={credits}
            tokens={tokens}
            spinSlots={spinSlots}
            spinRoulette={spinRoulette}
            blackjackDeal={blackjackDeal}
            blackjackHit={blackjackHit}
            blackjackStand={blackjackStand}
            blackjackDouble={blackjackDouble}
          />
        )}
        {view === "prizes" && (
          <PrizesView
            offline={offline}
            isAdmin={isAdmin}
            credits={credits}
            tokens={tokens}
            prizes={prizes}
            prizeLog={prizeLog}
            redeem={redeemPrize}
            addPrize={addPrize}
            deletePrize={deletePrize}
            convertToTokens={convertToTokens}
          />
        )}
        {view === "admin" && isAdmin && (
          <AdminView addPrize={addPrize} deletePrize={deletePrize} ownUsername={username} />
        )}
        {view === "stats" && (
          <StatsView
            offline={offline}
            totalStudied={totalStudied}
            bySubject={bySubject}
            sessions={sessions}
            credits={credits}
            tokens={tokens}
            prizeLog={prizeLog}
            exportData={exportData}
            importData={importData}
            resetData={resetData}
            addPastSession={addPastSession}
            editSession={editSession}
            deleteSession={deleteSession}
          />
        )}
      </main>
    </div>
  );
}

function CasinoView({
  offline,
  credits,
  tokens,
  spinSlots,
  spinRoulette,
  blackjackDeal,
  blackjackHit,
  blackjackStand,
  blackjackDouble,
}) {
  const [game, setGame] = useState("roulette");
  const gameProps = {
    offline,
    credits,
    tokens,
    spinSlots,
    spinRoulette,
    blackjackDeal,
    blackjackHit,
    blackjackStand,
    blackjackDouble,
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "center", marginBottom: 28, flexWrap: "wrap" }}>
        {["roulette", "blackjack", "slots"].map((g) => (
          <button
            key={g}
            onClick={() => setGame(g)}
            className="display-font"
            style={{
              padding: "10px 24px",
              background: game === g ? COLORS.gold : "transparent",
              color: game === g ? COLORS.feltDeep : COLORS.cream,
              border: `1px solid ${COLORS.gold}`,
              fontSize: 15,
              letterSpacing: "0.25em",
              textTransform: "uppercase",
              cursor: "pointer",
              fontWeight: 600,
            }}
          >
            {g}
          </button>
        ))}
      </div>

      <div
        style={{ fontSize: 12, color: COLORS.creamDim, textAlign: "center", marginBottom: 18, letterSpacing: "0.1em" }}
      >
        Bets pay out in <strong style={{ color: COLORS.rose }}>tokens</strong> · winnings can only be spent on prizes
      </div>

      {game === "roulette" && <Roulette {...gameProps} />}
      {game === "blackjack" && <Blackjack {...gameProps} />}
      {game === "slots" && <Slots {...gameProps} />}
    </div>
  );
}
