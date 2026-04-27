import React, { useState, useEffect, useRef, useMemo } from "react";

import { SyncIcon } from "./SyncBanner.jsx";
import { useCasino } from "./use_casino.js";

const SUBJECTS = [
  "Biochemistry",
  "Anatomy",
  "Physiology",
  "Immunology",
  "Microbiology",
  "Pathophysiology",
  "Pharmacology",
  "Biostatistics & Epi",
  "OMM",
];

// European roulette wheel pocket order (clockwise from 0)
const WHEEL = [
  0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7,
  28, 12, 35, 3, 26,
];
const RED = new Set([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]);

const DEFAULT_PRIZES = [
  { id: "p1", name: "Anime episode break", cost: 30 },
  { id: "p2", name: "Nice coffee shop trip", cost: 60 },
  { id: "p3", name: "Takeout night", cost: 120 },
  { id: "p4", name: "Nice dinner out with Rai", cost: 240 },
  { id: "p5", name: "Buy a new game", cost: 600 },
  { id: "p6", name: "Weekend getaway", cost: 1800 },
];

const SLOT_SYMBOLS = [
  { id: "seven", glyph: "7", color: "#e8b84a", weight: 1, payout: 50 },
  { id: "star", glyph: "★", color: "#e8b84a", weight: 3, payout: 20 },
  { id: "diamond", glyph: "◆", color: "#6fc4e8", weight: 5, payout: 10 },
  { id: "spade", glyph: "♠", color: "#f5e8c7", weight: 9, payout: 5 },
  { id: "club", glyph: "♣", color: "#f5e8c7", weight: 14, payout: 3 },
];

const CARD_SUITS = ["♠", "♥", "♦", "♣"];
const CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"];
const BLACKJACK_DECKS = 4;

function makeShoe(decks = BLACKJACK_DECKS) {
  const cards = [];
  for (let d = 0; d < decks; d++) {
    for (const s of CARD_SUITS) {
      for (const r of CARD_RANKS) {
        cards.push({ suit: s, rank: r, id: `${d}-${s}-${r}-${Math.random().toString(36).slice(2, 8)}` });
      }
    }
  }
  for (let i = cards.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [cards[i], cards[j]] = [cards[j], cards[i]];
  }
  return cards;
}

function cardRankValue(rank) {
  if (rank === "A") return 11;
  if (["J", "Q", "K"].includes(rank)) return 10;
  return parseInt(rank, 10);
}

function handValue(cards) {
  let total = 0;
  let aces = 0;
  for (const c of cards) {
    total += cardRankValue(c.rank);
    if (c.rank === "A") aces++;
  }
  while (total > 21 && aces > 0) {
    total -= 10;
    aces--;
  }
  return total;
}

function isBlackjack(cards) {
  return cards.length === 2 && handValue(cards) === 21;
}

function weightedPick(items) {
  const total = items.reduce((s, i) => s + i.weight, 0);
  let r = Math.random() * total;
  for (const it of items) {
    r -= it.weight;
    if (r <= 0) return it;
  }
  return items[items.length - 1];
}

function numColor(n) {
  if (n === 0) return "green";
  return RED.has(n) ? "red" : "black";
}

function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function fmtHoursMin(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h === 0) return `${m}m`;
  return `${h}h ${m}m`;
}

function getElapsedSec(session, now = Date.now()) {
  if (!session) return 0;
  let ms = now - session.startTime - (session.pausedDuration || 0);
  if (session.paused && session.pauseStartedAt) ms -= now - session.pauseStartedAt;
  return Math.max(0, ms / 1000);
}

// Red-and-gold casino palette. The felt is a deep crimson; gold and cream are
// the legible foreground accents. `wine` is a slightly brighter accent that
// lifts off the felt; `red` is reserved for danger affordances and roulette
// pockets so it has to remain visibly distinct from the felt's crimson.
const COLORS = {
  felt: "#5a1f2a",
  feltDark: "#3d1520",
  feltDeep: "#1f0a10",
  gold: "#d4a548",
  goldBright: "#e8b84a",
  goldDim: "#9a7a34",
  cream: "#f5e8c7",
  creamDim: "#c9bc9a",
  wine: "#7a2838",
  red: "#d44040",
  rose: "#e8b4c0",
  black: "#1a1a1a",
};

export default function StudyCasino() {
  const [view, setView] = useState("study");
  // Y.Doc-backed reactive state + every mutation function we need. The
  // single hook replaces the legacy useState/applySnapshot/emitEvents
  // machinery; multi-device sync now happens through the websocket-less
  // HTTP poll provider in `sync.js`. See use_casino.js for the doc shape.
  const casino = useCasino();
  const {
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
    addTokens,
    addCredits,
    exportData,
    importData,
    resetData,
  } = casino;

  // Local-only ticker for the live timer display so the elapsed-second
  // readout updates without touching the doc.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!activeSession || activeSession.paused) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [activeSession?.startTime, activeSession?.paused]);

  // === Derived values ===
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
        /* Coins/sparkles fan out from the center of a winning panel and fall.
           Each particle gets random end-position via CSS variables set inline. */
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

        @keyframes sync-spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
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
          {["study", "casino", "prizes", "stats"].map((v) => (
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
              style={{
                padding: "6px 14px",
                fontSize: 12,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                background: "transparent",
                color: COLORS.gold,
                border: `1px solid ${COLORS.gold}`,
                cursor: "pointer",
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
        {view === "casino" && <CasinoView credits={credits} addCredits={addCredits} addTokens={addTokens} />}
        {view === "prizes" && (
          <PrizesView
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
        {view === "stats" && (
          <StatsView
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

// ============================================================
// STUDY VIEW
// ============================================================
function StudyView({
  activeSession,
  activeElapsed,
  activeMinutes,
  todayTotal,
  sessions,
  credits,
  start,
  pause,
  resume,
  stop,
  cancel,
}) {
  return (
    <div>
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
                className="deco-corners"
                style={{
                  padding: "24px 16px",
                  background: "rgba(0,0,0,0.25)",
                  border: `1px solid rgba(212,165,72,0.3)`,
                  color: COLORS.cream,
                  cursor: "pointer",
                  fontFamily: "'Playfair Display', serif",
                  fontSize: 16,
                  fontWeight: 600,
                  letterSpacing: "0.05em",
                  transition: "all 0.2s",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "rgba(212,165,72,0.12)";
                  e.currentTarget.style.borderColor = COLORS.gold;
                }}
                onMouseLeave={(e) => {
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
            <button className="btn btn-primary" onClick={stop}>
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
        <StatCard label="Credit balance" value={credits.toLocaleString()} accent />
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

// ============================================================
// CASINO VIEW
// ============================================================
function CasinoView({ credits, addCredits, addTokens }) {
  const [game, setGame] = useState("roulette");
  const gameProps = { credits, addCredits, addTokens };

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

// ============================================================
// ROULETTE
// ============================================================
function Roulette({ credits, addCredits, addTokens }) {
  const [betAmount, setBetAmount] = useState(10);
  const [betType, setBetType] = useState("red"); // red, black, odd, even, low, high, dozen1, dozen2, dozen3, number
  const [betNumber, setBetNumber] = useState(7);
  const [spinning, setSpinning] = useState(false);
  const [result, setResult] = useState(null); // { number, won, payout }
  const [rotation, setRotation] = useState(0);
  const [history, setHistory] = useState([]);
  const [winBurst, setWinBurst] = useState(null);

  const canSpin = !spinning && betAmount > 0 && betAmount <= credits;

  const checkWin = (num) => {
    if (num === 0 && betType !== "number") return { won: false, mult: 0 };
    switch (betType) {
      case "red":
        return { won: RED.has(num), mult: 2 };
      case "black":
        return { won: num !== 0 && !RED.has(num), mult: 2 };
      case "odd":
        return { won: num % 2 === 1, mult: 2 };
      case "even":
        return { won: num !== 0 && num % 2 === 0, mult: 2 };
      case "low":
        return { won: num >= 1 && num <= 18, mult: 2 };
      case "high":
        return { won: num >= 19 && num <= 36, mult: 2 };
      case "dozen1":
        return { won: num >= 1 && num <= 12, mult: 3 };
      case "dozen2":
        return { won: num >= 13 && num <= 24, mult: 3 };
      case "dozen3":
        return { won: num >= 25 && num <= 36, mult: 3 };
      case "number":
        return { won: num === betNumber, mult: 36 };
      default:
        return { won: false, mult: 0 };
    }
  };

  const spin = () => {
    if (!canSpin) return;
    const pickedIdx = Math.floor(Math.random() * WHEEL.length);
    const picked = WHEEL[pickedIdx];

    // Take the bet up front against the doc; the local Y.Doc updates
    // synchronously so the UI re-renders immediately, and sync.js pushes
    // it to the server in the background.
    addCredits(-betAmount);
    setSpinning(true);
    setResult(null);

    // Rotation: add a random number of full rotations, then stop on the picked pocket
    const anglePer = 360 / WHEEL.length;
    const targetAngle = -(pickedIdx * anglePer); // minus because we rotate the wheel; pointer is fixed at top
    const fullSpins = 6 + Math.floor(Math.random() * 3);
    const finalRotation = rotation - fullSpins * 360 + (targetAngle - (rotation % 360));
    setRotation(finalRotation);

    setTimeout(() => {
      const w = checkWin(picked);
      const grossPayout = w.won ? betAmount * w.mult : 0;
      // Whole payout becomes tokens; the bet was already debited from credits.
      // The casino is a pure credits→tokens funnel — see validators.py.
      if (grossPayout > 0) {
        addTokens(grossPayout);
        setWinBurst({ key: Date.now(), amount: grossPayout });
      }
      setResult({ number: picked, won: w.won, payout: grossPayout });
      setHistory((h) => [{ number: picked, won: w.won }, ...h].slice(0, 10));
      setSpinning(false);
    }, 4200);
  };

  const BetTypeBtn = ({ value, children, size = "md" }) => (
    <button
      onClick={() => setBetType(value)}
      disabled={spinning}
      style={{
        padding: size === "sm" ? "6px 10px" : "10px 14px",
        background: betType === value ? COLORS.gold : "rgba(0,0,0,0.3)",
        color: betType === value ? COLORS.feltDeep : COLORS.cream,
        border: `1px solid ${betType === value ? COLORS.gold : "rgba(212,165,72,0.4)"}`,
        cursor: spinning ? "not-allowed" : "pointer",
        fontFamily: "'Outfit', sans-serif",
        fontSize: size === "sm" ? 12 : 13,
        fontWeight: 500,
        letterSpacing: "0.05em",
        textTransform: "uppercase",
        transition: "all 0.15s",
        opacity: spinning ? 0.6 : 1,
      }}
    >
      {children}
    </button>
  );

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(280px, 360px)", gap: 24 }}>
      {/* Wheel */}
      <div className="panel deco-corners" style={{ padding: 32, textAlign: "center", position: "relative" }}>
        {winBurst && <WinBurst key={winBurst.key} amount={winBurst.amount} />}
        <div style={{ position: "relative", width: 280, height: 280, margin: "0 auto" }}>
          {/* Pointer */}
          <div
            style={{
              position: "absolute",
              top: -2,
              left: "50%",
              transform: "translateX(-50%)",
              width: 0,
              height: 0,
              borderLeft: "10px solid transparent",
              borderRight: "10px solid transparent",
              borderTop: `18px solid ${COLORS.gold}`,
              zIndex: 10,
            }}
          />
          {/* Wheel */}
          <svg
            viewBox="-150 -150 300 300"
            style={{
              width: "100%",
              height: "100%",
              transform: `rotate(${rotation}deg)`,
              transition: spinning ? "transform 4s cubic-bezier(0.15, 0.7, 0.25, 1)" : "none",
              filter: "drop-shadow(0 4px 12px rgba(0,0,0,0.5))",
            }}
          >
            <circle r="140" fill={COLORS.goldDim} />
            <circle r="136" fill={COLORS.black} />
            {WHEEL.map((num, i) => {
              const anglePer = 360 / WHEEL.length;
              const startAngle = i * anglePer - 90 - anglePer / 2;
              const endAngle = startAngle + anglePer;
              const sRad = (startAngle * Math.PI) / 180;
              const eRad = (endAngle * Math.PI) / 180;
              const r = 130;
              const x1 = Math.cos(sRad) * r,
                y1 = Math.sin(sRad) * r;
              const x2 = Math.cos(eRad) * r,
                y2 = Math.sin(eRad) * r;
              const fill = num === 0 ? "#1e6e3e" : RED.has(num) ? COLORS.red : COLORS.black;
              const textAngle = i * anglePer - 90;
              const tx = Math.cos((textAngle * Math.PI) / 180) * 108;
              const ty = Math.sin((textAngle * Math.PI) / 180) * 108;
              return (
                <g key={i}>
                  <path
                    d={`M 0 0 L ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2} Z`}
                    fill={fill}
                    stroke={COLORS.goldDim}
                    strokeWidth="0.5"
                  />
                  <text
                    x={tx}
                    y={ty}
                    textAnchor="middle"
                    dominantBaseline="central"
                    fill={COLORS.cream}
                    fontSize="10"
                    fontWeight="700"
                    transform={`rotate(${textAngle + 90} ${tx} ${ty})`}
                  >
                    {num}
                  </text>
                </g>
              );
            })}
            <circle r="40" fill={COLORS.goldDim} />
            <circle r="36" fill={COLORS.gold} />
            <text
              x="0"
              y="0"
              textAnchor="middle"
              dominantBaseline="central"
              fill={COLORS.feltDeep}
              fontSize="14"
              fontWeight="700"
              fontFamily="Playfair Display, serif"
            >
              ROULETTE
            </text>
          </svg>
        </div>

        {result && !spinning && (
          <div style={{ marginTop: 20 }}>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                padding: "8px 20px",
                background:
                  numColor(result.number) === "red"
                    ? COLORS.red
                    : numColor(result.number) === "black"
                      ? COLORS.black
                      : "#1e6e3e",
                border: `1px solid ${COLORS.gold}`,
                color: COLORS.cream,
                fontWeight: 700,
                fontFamily: "'Playfair Display', serif",
                fontSize: 20,
              }}
            >
              {result.number}
            </div>
            <div
              style={{
                marginTop: 12,
                fontSize: 18,
                color: result.won ? COLORS.goldBright : COLORS.creamDim,
                fontFamily: "'Playfair Display', serif",
                letterSpacing: "0.1em",
              }}
            >
              {result.won ? `WIN +${result.payout} tokens` : "LOST"}
            </div>
          </div>
        )}
        {spinning && (
          <div
            style={{
              marginTop: 20,
              fontSize: 14,
              color: COLORS.creamDim,
              letterSpacing: "0.2em",
              textTransform: "uppercase",
            }}
          >
            Spinning...
          </div>
        )}

        {history.length > 0 && (
          <div style={{ marginTop: 24, display: "flex", gap: 4, justifyContent: "center", flexWrap: "wrap" }}>
            {history.map((h, i) => (
              <div
                key={i}
                style={{
                  width: 24,
                  height: 24,
                  borderRadius: "50%",
                  background:
                    numColor(h.number) === "red"
                      ? COLORS.red
                      : numColor(h.number) === "black"
                        ? COLORS.black
                        : "#1e6e3e",
                  color: COLORS.cream,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 10,
                  fontWeight: 700,
                  border: `1px solid ${COLORS.goldDim}`,
                  opacity: 1 - i * 0.08,
                }}
              >
                {h.number}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Betting controls */}
      <div className="panel" style={{ padding: 20 }}>
        <SectionTitle small>Your Bet</SectionTitle>

        <div style={{ marginBottom: 16 }}>
          <div
            style={{
              fontSize: 11,
              color: COLORS.creamDim,
              letterSpacing: "0.15em",
              textTransform: "uppercase",
              marginBottom: 6,
            }}
          >
            Amount
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <input
              type="number"
              value={betAmount}
              onChange={(e) => setBetAmount(Math.max(1, Math.min(credits, parseInt(e.target.value) || 0)))}
              disabled={spinning}
              min="1"
              max={credits}
              style={{ flex: 1 }}
            />
          </div>
          <div style={{ display: "flex", gap: 4, marginTop: 6, flexWrap: "wrap" }}>
            {[5, 10, 25, 50, 100].map((v) => (
              <button
                key={v}
                onClick={() => setBetAmount(Math.min(v, credits))}
                disabled={spinning || credits < v}
                style={{
                  padding: "4px 10px",
                  fontSize: 11,
                  background: "transparent",
                  color: COLORS.creamDim,
                  border: `1px solid rgba(212,165,72,0.3)`,
                  cursor: spinning || credits < v ? "not-allowed" : "pointer",
                  opacity: spinning || credits < v ? 0.4 : 1,
                }}
              >
                {v}
              </button>
            ))}
            <button
              onClick={() => setBetAmount(credits)}
              disabled={spinning || credits === 0}
              style={{
                padding: "4px 10px",
                fontSize: 11,
                background: "transparent",
                color: COLORS.gold,
                border: `1px solid ${COLORS.gold}`,
                cursor: spinning || credits === 0 ? "not-allowed" : "pointer",
                opacity: spinning || credits === 0 ? 0.4 : 1,
              }}
            >
              ALL
            </button>
          </div>
        </div>

        <div style={{ marginBottom: 16 }}>
          <div
            style={{
              fontSize: 11,
              color: COLORS.creamDim,
              letterSpacing: "0.15em",
              textTransform: "uppercase",
              marginBottom: 6,
            }}
          >
            Bet type{" "}
            <span style={{ color: COLORS.gold }}>
              · pays {betType === "number" ? "35:1" : betType.startsWith("dozen") ? "2:1" : "1:1"}
            </span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
            <BetTypeBtn value="red">
              <span style={{ color: betType === "red" ? COLORS.feltDeep : COLORS.red }}>●</span> Red
            </BetTypeBtn>
            <BetTypeBtn value="black">
              <span>●</span> Black
            </BetTypeBtn>
            <BetTypeBtn value="odd">Odd</BetTypeBtn>
            <BetTypeBtn value="even">Even</BetTypeBtn>
            <BetTypeBtn value="low">1–18</BetTypeBtn>
            <BetTypeBtn value="high">19–36</BetTypeBtn>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4, marginTop: 4 }}>
            <BetTypeBtn value="dozen1" size="sm">
              1st 12
            </BetTypeBtn>
            <BetTypeBtn value="dozen2" size="sm">
              2nd 12
            </BetTypeBtn>
            <BetTypeBtn value="dozen3" size="sm">
              3rd 12
            </BetTypeBtn>
          </div>
          <div style={{ marginTop: 8 }}>
            <BetTypeBtn value="number">Single Number (35:1)</BetTypeBtn>
            {betType === "number" && (
              <div style={{ marginTop: 8 }}>
                <input
                  type="number"
                  value={betNumber}
                  onChange={(e) => setBetNumber(Math.max(0, Math.min(36, parseInt(e.target.value) || 0)))}
                  disabled={spinning}
                  min="0"
                  max="36"
                  style={{ width: "100%" }}
                  placeholder="0-36"
                />
              </div>
            )}
          </div>
        </div>

        <button
          className="btn btn-primary"
          style={{ width: "100%", padding: "14px", fontSize: 15 }}
          onClick={spin}
          disabled={!canSpin}
        >
          {spinning ? "Spinning..." : `Spin · ${betAmount} cr`}
        </button>

        {credits === 0 && (
          <div style={{ marginTop: 12, textAlign: "center", fontSize: 12, color: COLORS.creamDim }}>
            Go study to earn credits
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// SLOTS
// ============================================================
function Slots({ credits, addCredits, addTokens }) {
  const [bet, setBet] = useState(5);
  const [targets, setTargets] = useState([SLOT_SYMBOLS[2], SLOT_SYMBOLS[3], SLOT_SYMBOLS[4]]);
  const [spinning, setSpinning] = useState(false);
  const [lastResult, setLastResult] = useState(null);
  const [winBurst, setWinBurst] = useState(null);

  const canSpin = !spinning && bet > 0 && bet <= credits;

  const spin = () => {
    if (!canSpin) return;
    addCredits(-bet);
    setSpinning(true);
    setLastResult(null);

    const picks = [weightedPick(SLOT_SYMBOLS), weightedPick(SLOT_SYMBOLS), weightedPick(SLOT_SYMBOLS)];
    setTargets(picks);

    setTimeout(() => {
      let grossPayout = 0;
      let label = "";
      const [a, b, c] = picks;
      if (a.id === b.id && b.id === c.id) {
        grossPayout = bet * a.payout;
        label = `Triple ${a.glyph} · ${a.payout}×`;
      } else if (a.id === b.id || b.id === c.id || a.id === c.id) {
        grossPayout = Math.floor(bet * 1.5);
        label = "Pair · 1.5×";
      } else {
        label = "No match";
      }
      if (grossPayout > 0) {
        addTokens(grossPayout);
        setWinBurst({ key: Date.now(), amount: grossPayout });
      }
      setLastResult({ picks, payout: grossPayout, label });
      setSpinning(false);
    }, 4000);
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(240px, 320px)", gap: 24 }}>
      <div className="panel deco-corners" style={{ padding: 32, textAlign: "center", position: "relative" }}>
        {winBurst && <WinBurst key={winBurst.key} amount={winBurst.amount} />}
        <div
          style={{
            display: "flex",
            gap: 12,
            justifyContent: "center",
            padding: 20,
            background: COLORS.feltDeep,
            border: `2px solid ${COLORS.gold}`,
            borderRadius: 4,
            marginBottom: 24,
          }}
        >
          {targets.map((t, i) => (
            <SlotReel key={i} target={t} index={i} spinning={spinning} />
          ))}
        </div>

        {lastResult && !spinning && (
          <div>
            <div
              className="display-font"
              style={{
                fontSize: 24,
                color: lastResult.payout > 0 ? COLORS.goldBright : COLORS.creamDim,
                letterSpacing: "0.15em",
                textTransform: "uppercase",
              }}
            >
              {lastResult.label}
            </div>
            {lastResult.payout > 0 && (
              <div style={{ fontSize: 28, color: COLORS.gold, fontWeight: 700, marginTop: 6 }}>
                +{lastResult.payout} tokens
              </div>
            )}
          </div>
        )}
      </div>

      <div>
        <div className="panel" style={{ padding: 20, marginBottom: 16 }}>
          <SectionTitle small>Your Bet</SectionTitle>
          <input
            type="number"
            value={bet}
            onChange={(e) => setBet(Math.max(1, Math.min(credits, parseInt(e.target.value) || 0)))}
            disabled={spinning}
            min="1"
            max={credits}
            style={{ width: "100%", marginBottom: 10 }}
          />
          <div style={{ display: "flex", gap: 4, marginBottom: 16, flexWrap: "wrap" }}>
            {[1, 5, 10, 25, 50].map((v) => (
              <button
                key={v}
                onClick={() => setBet(Math.min(v, credits))}
                disabled={spinning || credits < v}
                style={{
                  padding: "4px 10px",
                  fontSize: 11,
                  background: "transparent",
                  color: COLORS.creamDim,
                  border: `1px solid rgba(212,165,72,0.3)`,
                  cursor: spinning || credits < v ? "not-allowed" : "pointer",
                  opacity: spinning || credits < v ? 0.4 : 1,
                }}
              >
                {v}
              </button>
            ))}
          </div>

          <button
            className="btn btn-primary"
            style={{ width: "100%", padding: "14px" }}
            onClick={spin}
            disabled={!canSpin}
          >
            {spinning ? "Spinning..." : `Spin · ${bet} cr`}
          </button>
        </div>

        <div className="panel" style={{ padding: 16 }}>
          <div
            style={{
              fontSize: 11,
              color: COLORS.gold,
              letterSpacing: "0.2em",
              textTransform: "uppercase",
              marginBottom: 10,
            }}
          >
            Payouts
          </div>
          {SLOT_SYMBOLS.map((s) => (
            <div
              key={s.id}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                padding: "4px 0",
                fontSize: 13,
              }}
            >
              <span
                style={{
                  fontSize: 22,
                  color: s.color,
                  fontFamily: "'Playfair Display', serif",
                  fontWeight: 700,
                  width: 32,
                }}
              >
                {s.glyph}
                {s.glyph}
                {s.glyph}
              </span>
              <span className="mono" style={{ color: COLORS.cream }}>
                {s.payout}×
              </span>
            </div>
          ))}
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              padding: "4px 0",
              fontSize: 13,
              borderTop: `1px solid rgba(212,165,72,0.2)`,
              marginTop: 6,
              paddingTop: 8,
            }}
          >
            <span style={{ color: COLORS.creamDim }}>Any pair</span>
            <span className="mono" style={{ color: COLORS.cream }}>
              1.5×
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function SlotReel({ target, index, spinning }) {
  const [displayed, setDisplayed] = useState(target);
  const [blurred, setBlurred] = useState(false);
  const targetRef = useRef(target);
  targetRef.current = target;

  useEffect(() => {
    if (!spinning) return;
    setBlurred(true);
    const interval = setInterval(() => {
      setDisplayed(SLOT_SYMBOLS[Math.floor(Math.random() * SLOT_SYMBOLS.length)]);
    }, 60);
    const stopAt = 2500 + index * 700;
    const timeout = setTimeout(() => {
      clearInterval(interval);
      setDisplayed(targetRef.current);
      setBlurred(false);
    }, stopAt);
    return () => {
      clearInterval(interval);
      clearTimeout(timeout);
    };
  }, [spinning, index]);

  useEffect(() => {
    if (!spinning) setDisplayed(target);
  }, [target, spinning]);

  return (
    <div
      style={{
        width: 100,
        height: 120,
        background: "linear-gradient(180deg, #080808 0%, #1a1a1a 50%, #080808 100%)",
        border: `1px solid ${COLORS.goldDim}`,
        borderRadius: 4,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        overflow: "hidden",
        position: "relative",
      }}
    >
      <div
        style={{
          fontSize: 64,
          fontFamily: "'Playfair Display', Didot, Georgia, serif",
          fontWeight: 700,
          color: displayed.color,
          lineHeight: 1,
          filter: blurred ? "blur(2px)" : "none",
          opacity: blurred ? 0.7 : 1,
          transition: blurred ? "none" : "filter 0.3s, opacity 0.3s",
        }}
      >
        {displayed.glyph}
      </div>
    </div>
  );
}

// ============================================================
// BLACKJACK
// ============================================================
function Blackjack({ credits, addCredits, addTokens }) {
  const [shoe, setShoe] = useState(() => makeShoe(BLACKJACK_DECKS));
  const [playerHand, setPlayerHand] = useState([]);
  const [dealerHand, setDealerHand] = useState([]);
  const [phase, setPhase] = useState("betting"); // betting | playing | dealer | done
  const [betInput, setBetInput] = useState(10);
  const [wager, setWager] = useState(0);
  const [result, setResult] = useState(null); // { outcome, payout, text }
  const [holeHidden, setHoleHidden] = useState(true);
  const [winBurst, setWinBurst] = useState(null);

  const playerValue = useMemo(() => handValue(playerHand), [playerHand]);
  const dealerValue = useMemo(() => handValue(dealerHand), [dealerHand]);
  const dealerVisibleValue = useMemo(() => {
    if (!holeHidden || dealerHand.length < 2) return handValue(dealerHand);
    return handValue([dealerHand[0]]);
  }, [dealerHand, holeHidden]);

  const canDeal = phase === "betting" && betInput > 0 && betInput <= credits;
  const canHit = phase === "playing" && playerValue < 21;
  const canStand = phase === "playing";
  const canDouble = phase === "playing" && playerHand.length === 2 && credits >= wager;

  const drawCards = (src, n) => {
    const drawn = src.slice(src.length - n);
    const remaining = src.slice(0, src.length - n);
    return [drawn, remaining];
  };

  const settle = (finalPlayer, finalDealer, currentWager) => {
    const pv = handValue(finalPlayer);
    const dv = handValue(finalDealer);
    const pBJ = isBlackjack(finalPlayer);
    const dBJ = isBlackjack(finalDealer);
    let outcome, payout, text;

    if (pv > 21) {
      outcome = "bust";
      payout = 0;
      text = "Bust. Dealer takes it.";
    } else if (pBJ && !dBJ) {
      outcome = "blackjack";
      payout = Math.floor(currentWager * 2.5);
      text = "Blackjack! Pays 3:2.";
    } else if (pBJ && dBJ) {
      outcome = "push";
      payout = currentWager;
      text = "Both blackjack. Push.";
    } else if (!pBJ && dBJ) {
      outcome = "lose";
      payout = 0;
      text = "Dealer blackjack.";
    } else if (dv > 21) {
      outcome = "dealerBust";
      payout = currentWager * 2;
      text = "Dealer busts. You win.";
    } else if (pv > dv) {
      outcome = "win";
      payout = currentWager * 2;
      text = "You win.";
    } else if (pv === dv) {
      outcome = "push";
      payout = currentWager;
      text = "Push.";
    } else {
      outcome = "lose";
      payout = 0;
      text = "Dealer wins.";
    }

    // Whole gross payout becomes tokens; bet was already debited at deal time.
    if (payout > 0) {
      addTokens(payout);
      setWinBurst({ key: Date.now(), amount: payout });
    }
    setResult({ outcome, payout, text });
    setPhase("done");
    setHoleHidden(false);
  };

  const playDealer = (currentDealer, currentShoe, finalPlayer, currentWager) => {
    setHoleHidden(false);
    const step = (hand, sh) => {
      if (handValue(hand) >= 17) {
        setTimeout(() => settle(finalPlayer, hand, currentWager), 500);
        return;
      }
      setTimeout(() => {
        const [drawn, rest] = drawCards(sh, 1);
        const newHand = [...hand, ...drawn];
        setDealerHand(newHand);
        setShoe(rest);
        step(newHand, rest);
      }, 650);
    };
    step(currentDealer, currentShoe);
  };

  const deal = () => {
    if (!canDeal) return;
    // Reshuffle if below cut card
    let workingShoe = shoe;
    if (workingShoe.length < 52) workingShoe = makeShoe(BLACKJACK_DECKS);

    addCredits(-betInput);
    setWager(betInput);
    setResult(null);
    setHoleHidden(true);

    const [p1, s1] = drawCards(workingShoe, 1);
    const [d1, s2] = drawCards(s1, 1);
    const [p2, s3] = drawCards(s2, 1);
    const [d2, s4] = drawCards(s3, 1);

    const ph = [...p1, ...p2];
    const dh = [...d1, ...d2];
    setPlayerHand(ph);
    setDealerHand(dh);
    setShoe(s4);

    // Check for immediate blackjack or dealer-up-card ace/ten (simplified: no insurance)
    if (isBlackjack(ph) || isBlackjack(dh)) {
      setTimeout(() => {
        setHoleHidden(false);
        setTimeout(() => settle(ph, dh, betInput), 500);
      }, 600);
      return;
    }

    setPhase("playing");
  };

  const hit = () => {
    if (!canHit) return;
    const [drawn, rest] = drawCards(shoe, 1);
    const newHand = [...playerHand, ...drawn];
    setPlayerHand(newHand);
    setShoe(rest);
    const v = handValue(newHand);
    if (v > 21) {
      setPhase("dealer");
      setTimeout(() => settle(newHand, dealerHand, wager), 600);
    } else if (v === 21) {
      setPhase("dealer");
      setTimeout(() => playDealer(dealerHand, rest, newHand, wager), 600);
    }
  };

  const stand = () => {
    if (!canStand) return;
    setPhase("dealer");
    setTimeout(() => playDealer(dealerHand, shoe, playerHand, wager), 400);
  };

  const doubleDown = () => {
    if (!canDouble) return;
    addCredits(-wager);
    const newWager = wager * 2;
    setWager(newWager);
    const [drawn, rest] = drawCards(shoe, 1);
    const newHand = [...playerHand, ...drawn];
    setPlayerHand(newHand);
    setShoe(rest);
    setPhase("dealer");
    const v = handValue(newHand);
    if (v > 21) {
      setTimeout(() => settle(newHand, dealerHand, newWager), 600);
    } else {
      setTimeout(() => playDealer(dealerHand, rest, newHand, newWager), 700);
    }
  };

  const newHand = () => {
    setPlayerHand([]);
    setDealerHand([]);
    setResult(null);
    setWager(0);
    setHoleHidden(true);
    setPhase("betting");
  };

  const resultColor = result
    ? ["blackjack", "win", "dealerBust"].includes(result.outcome)
      ? COLORS.goldBright
      : result.outcome === "push"
        ? COLORS.cream
        : COLORS.creamDim
    : COLORS.cream;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(260px, 340px)", gap: 24 }}>
      <div className="panel deco-corners" style={{ padding: 28, minHeight: 420, position: "relative" }}>
        {winBurst && <WinBurst key={winBurst.key} amount={winBurst.amount} />}
        {/* Dealer */}
        <div style={{ marginBottom: 28 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div
              className="display-font"
              style={{ fontSize: 13, letterSpacing: "0.25em", textTransform: "uppercase", color: COLORS.creamDim }}
            >
              Dealer
            </div>
            <div className="mono" style={{ fontSize: 15, color: COLORS.cream }}>
              {dealerHand.length > 0 ? (holeHidden ? `${dealerVisibleValue} + ?` : dealerValue) : ""}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, minHeight: 92, flexWrap: "wrap" }}>
            {dealerHand.map((c, i) => (
              <PlayingCard key={c.id} card={c} hidden={i === 1 && holeHidden} />
            ))}
          </div>
        </div>

        {/* Player */}
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div
              className="display-font"
              style={{ fontSize: 13, letterSpacing: "0.25em", textTransform: "uppercase", color: COLORS.creamDim }}
            >
              Player
            </div>
            <div className="mono" style={{ fontSize: 15, color: playerValue > 21 ? COLORS.red : COLORS.cream }}>
              {playerHand.length > 0 ? playerValue : ""}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, minHeight: 92, flexWrap: "wrap" }}>
            {playerHand.map((c) => (
              <PlayingCard key={c.id} card={c} />
            ))}
          </div>
        </div>

        {result && (
          <div
            style={{
              marginTop: 24,
              padding: 14,
              textAlign: "center",
              border: `1px solid ${resultColor}`,
              background: "rgba(0,0,0,0.3)",
            }}
          >
            <div className="display-font" style={{ fontSize: 18, color: resultColor, letterSpacing: "0.1em" }}>
              {result.text}
            </div>
            {result.payout > 0 && (
              <div style={{ fontSize: 14, color: COLORS.rose, marginTop: 4 }}>+{result.payout} tokens</div>
            )}
          </div>
        )}
      </div>

      <div>
        <div className="panel" style={{ padding: 18, marginBottom: 12 }}>
          <SectionTitle small>Wager</SectionTitle>

          {phase === "betting" ? (
            <>
              <input
                type="number"
                value={betInput}
                onChange={(e) => setBetInput(Math.max(1, Math.min(credits, parseInt(e.target.value) || 0)))}
                min="1"
                max={credits}
                style={{ width: "100%", marginBottom: 8 }}
              />
              <div style={{ display: "flex", gap: 4, marginBottom: 12, flexWrap: "wrap" }}>
                {[5, 10, 25, 50, 100].map((v) => (
                  <button
                    key={v}
                    onClick={() => setBetInput(Math.min(v, credits))}
                    disabled={credits < v}
                    style={{
                      padding: "4px 10px",
                      fontSize: 11,
                      background: "transparent",
                      color: COLORS.creamDim,
                      border: `1px solid rgba(212,165,72,0.3)`,
                      cursor: credits < v ? "not-allowed" : "pointer",
                      opacity: credits < v ? 0.4 : 1,
                    }}
                  >
                    {v}
                  </button>
                ))}
              </div>
              <button
                className="btn btn-primary"
                style={{ width: "100%", padding: 12 }}
                onClick={deal}
                disabled={!canDeal}
              >
                Deal · {betInput} cr
              </button>
            </>
          ) : (
            <>
              <div style={{ fontSize: 13, color: COLORS.creamDim, marginBottom: 4 }}>Current wager</div>
              <div
                className="display-font mono"
                style={{ fontSize: 24, color: COLORS.gold, fontWeight: 700, marginBottom: 14 }}
              >
                {wager}
              </div>

              {phase === "playing" && (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 6 }}>
                  <button className="btn btn-primary" style={{ padding: 10 }} onClick={hit} disabled={!canHit}>
                    Hit
                  </button>
                  <button className="btn btn-primary" style={{ padding: 10 }} onClick={stand} disabled={!canStand}>
                    Stand
                  </button>
                  <button
                    className="btn"
                    style={{ padding: 10, gridColumn: "span 2" }}
                    onClick={doubleDown}
                    disabled={!canDouble}
                  >
                    Double {credits < wager ? "(not enough credits)" : `(+${wager})`}
                  </button>
                </div>
              )}

              {phase === "dealer" && (
                <div
                  style={{
                    fontSize: 12,
                    color: COLORS.creamDim,
                    textAlign: "center",
                    padding: "12px 0",
                    letterSpacing: "0.15em",
                    textTransform: "uppercase",
                  }}
                >
                  Dealer plays...
                </div>
              )}

              {phase === "done" && (
                <button className="btn btn-primary" style={{ width: "100%", padding: 12 }} onClick={newHand}>
                  New hand
                </button>
              )}
            </>
          )}
        </div>

        <div className="panel" style={{ padding: 14 }}>
          <div
            style={{
              fontSize: 10,
              color: COLORS.gold,
              letterSpacing: "0.2em",
              textTransform: "uppercase",
              marginBottom: 8,
            }}
          >
            Rules
          </div>
          <div style={{ fontSize: 11, color: COLORS.creamDim, lineHeight: 1.7 }}>
            4-deck shoe · Dealer stands on all 17 · Blackjack pays 3:2 · Double on any 2 cards · No split · No insurance
          </div>
          <div
            style={{
              fontSize: 10,
              color: COLORS.creamDim,
              marginTop: 10,
              letterSpacing: "0.15em",
              textTransform: "uppercase",
            }}
          >
            Shoe: {shoe.length} / {BLACKJACK_DECKS * 52} cards
          </div>
        </div>
      </div>
    </div>
  );
}

function PlayingCard({ card, hidden }) {
  if (hidden) {
    return (
      <div
        style={{
          width: 62,
          height: 88,
          background: `repeating-linear-gradient(45deg, ${COLORS.wine}, ${COLORS.wine} 6px, #3d1520 6px, #3d1520 12px)`,
          border: `1.5px solid ${COLORS.gold}`,
          borderRadius: 6,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <div style={{ width: 32, height: 48, border: `1px solid ${COLORS.goldDim}`, opacity: 0.6 }} />
      </div>
    );
  }
  const isRed = card.suit === "♥" || card.suit === "♦";
  const color = isRed ? "#c23030" : "#1a1a1a";
  return (
    <div
      style={{
        width: 62,
        height: 88,
        background: "#faf6ec",
        border: `1px solid #bba372`,
        borderRadius: 6,
        color,
        fontFamily: "'Playfair Display', Didot, Georgia, serif",
        fontWeight: 700,
        padding: "5px 6px",
        display: "flex",
        flexDirection: "column",
        position: "relative",
        boxShadow: "0 2px 4px rgba(0,0,0,0.3)",
      }}
    >
      <div style={{ fontSize: 13, lineHeight: 1 }}>
        <div>{card.rank}</div>
        <div style={{ fontSize: 11 }}>{card.suit}</div>
      </div>
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          fontSize: 28,
        }}
      >
        {card.suit}
      </div>
      <div
        style={{
          position: "absolute",
          bottom: 5,
          right: 6,
          fontSize: 13,
          lineHeight: 1,
          transform: "rotate(180deg)",
          transformOrigin: "center",
        }}
      >
        <div>{card.rank}</div>
        <div style={{ fontSize: 11 }}>{card.suit}</div>
      </div>
    </div>
  );
}

// ============================================================
// PRIZES VIEW
// ============================================================
function PrizesView({ credits, tokens, prizes, prizeLog, redeem, addPrize, deletePrize, convertToTokens }) {
  const [newName, setNewName] = useState("");
  const [newCost, setNewCost] = useState("");
  const [confirming, setConfirming] = useState(null);
  const [convertAmount, setConvertAmount] = useState("");

  const handleAdd = () => {
    const cost = parseInt(newCost);
    if (!newName.trim() || !cost || cost <= 0) return;
    addPrize(newName.trim(), cost);
    setNewName("");
    setNewCost("");
  };

  const handleConvert = () => {
    const n = parseInt(convertAmount);
    if (!n || n <= 0 || n > credits) return;
    convertToTokens(n);
    setConvertAmount("");
  };

  const pinkText = "#e8b4c0";
  const pinkBg = "rgba(90,31,42,0.35)";

  return (
    <div>
      <SectionTitle>The Vault</SectionTitle>

      <div
        className="panel deco-corners"
        style={{ padding: 20, marginBottom: 32, background: "rgba(90,31,42,0.18)", borderColor: COLORS.wine }}
      >
        <div style={{ fontSize: 13, color: COLORS.creamDim, marginBottom: 14, lineHeight: 1.6 }}>
          Casino winnings already arrive as <strong style={{ color: pinkText, fontWeight: 500 }}>tokens</strong>, ready
          to redeem. You can also pre-convert raw study credits if you want to lock them in beyond the casino's reach.{" "}
          <strong style={{ color: pinkText, fontWeight: 500 }}>Tokens are one-way</strong> — they can only be spent on
          prizes.
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 10, alignItems: "end" }}>
          <div>
            <div
              style={{
                fontSize: 10,
                color: COLORS.creamDim,
                letterSpacing: "0.2em",
                textTransform: "uppercase",
                marginBottom: 4,
              }}
            >
              Credits available
            </div>
            <div className="display-font mono" style={{ fontSize: 22, color: COLORS.gold, fontWeight: 700 }}>
              {credits.toLocaleString()}
            </div>
          </div>
          <input
            type="number"
            placeholder="Amount to convert"
            value={convertAmount}
            onChange={(e) => setConvertAmount(e.target.value)}
            min="1"
            max={credits}
          />
          <button
            className="btn btn-primary"
            onClick={handleConvert}
            disabled={!parseInt(convertAmount) || parseInt(convertAmount) > credits}
          >
            Convert to tokens
          </button>
        </div>
        <div style={{ display: "flex", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
          <span style={{ fontSize: 11, color: COLORS.creamDim, marginRight: 8, alignSelf: "center" }}>Quick:</span>
          {[25, 50, 100, 250, 500]
            .filter((n) => n <= credits)
            .map((n) => (
              <button
                key={n}
                onClick={() => setConvertAmount(String(n))}
                style={{
                  padding: "4px 10px",
                  fontSize: 11,
                  background: "transparent",
                  color: COLORS.creamDim,
                  border: `1px solid rgba(212,165,72,0.25)`,
                  cursor: "pointer",
                }}
              >
                {n}
              </button>
            ))}
          {credits > 0 && (
            <button
              onClick={() => setConvertAmount(String(credits))}
              style={{
                padding: "4px 10px",
                fontSize: 11,
                background: "transparent",
                color: pinkText,
                border: `1px solid ${COLORS.wine}`,
                cursor: "pointer",
              }}
            >
              All ({credits})
            </button>
          )}
        </div>
      </div>

      <SectionTitle>Prizes</SectionTitle>
      <div style={{ fontSize: 12, color: COLORS.creamDim, marginBottom: 16, letterSpacing: "0.05em" }}>
        You have <strong style={{ color: pinkText }}>{tokens.toLocaleString()}</strong> tokens to spend.
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
          gap: 12,
          marginBottom: 40,
        }}
      >
        {prizes.map((p) => {
          const canAfford = tokens >= p.cost;
          const isConfirming = confirming === p.id;
          return (
            <div
              key={p.id}
              className="deco-corners"
              style={{
                padding: 20,
                background: canAfford ? pinkBg : "rgba(0,0,0,0.3)",
                border: `1px solid ${canAfford ? COLORS.wine : "rgba(212,165,72,0.2)"}`,
                position: "relative",
              }}
            >
              <button
                onClick={() => deletePrize(p.id)}
                style={{
                  position: "absolute",
                  top: 6,
                  right: 6,
                  background: "transparent",
                  border: "none",
                  color: COLORS.creamDim,
                  cursor: "pointer",
                  fontSize: 16,
                  padding: 4,
                  lineHeight: 1,
                }}
                title="Delete"
              >
                ×
              </button>
              <div
                className="display-font"
                style={{
                  fontSize: 18,
                  color: COLORS.cream,
                  marginBottom: 10,
                  minHeight: 48,
                }}
              >
                {p.name}
              </div>
              <div
                style={{
                  fontSize: 13,
                  color: canAfford ? pinkText : COLORS.creamDim,
                  letterSpacing: "0.15em",
                  textTransform: "uppercase",
                  marginBottom: 14,
                }}
              >
                {p.cost.toLocaleString()} tokens · {fmtHoursMin(p.cost * 60)}
              </div>
              {isConfirming ? (
                <div style={{ display: "flex", gap: 6 }}>
                  <button
                    className="btn btn-primary"
                    style={{ flex: 1, padding: "8px", fontSize: 11 }}
                    onClick={() => {
                      redeem(p);
                      setConfirming(null);
                    }}
                  >
                    Confirm
                  </button>
                  <button
                    className="btn"
                    style={{ flex: 1, padding: "8px", fontSize: 11 }}
                    onClick={() => setConfirming(null)}
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  className={canAfford ? "btn btn-primary" : "btn"}
                  style={{ width: "100%", padding: "8px" }}
                  disabled={!canAfford}
                  onClick={() => setConfirming(p.id)}
                >
                  {canAfford ? "Redeem" : `Need ${p.cost - tokens} more tokens`}
                </button>
              )}
            </div>
          );
        })}
      </div>

      <SectionTitle>Add a prize</SectionTitle>
      <div className="panel" style={{ padding: 20, marginBottom: 40 }}>
        <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr auto", gap: 10 }}>
          <input placeholder="Prize name" value={newName} onChange={(e) => setNewName(e.target.value)} />
          <input
            type="number"
            placeholder="Cost (tokens)"
            value={newCost}
            onChange={(e) => setNewCost(e.target.value)}
          />
          <button className="btn btn-primary" onClick={handleAdd} disabled={!newName.trim() || !parseInt(newCost)}>
            Add
          </button>
        </div>
        <div style={{ fontSize: 12, color: COLORS.creamDim, marginTop: 10 }}>
          Prize cost is in tokens. Since 1 credit = 1 minute of study and 1 credit converts to 1 token, a 120-token
          prize costs 2 hours of study.
        </div>
      </div>

      {prizeLog.length > 0 && (
        <>
          <SectionTitle>Claimed</SectionTitle>
          <div className="panel" style={{ padding: 0 }}>
            {prizeLog.slice(0, 10).map((p, i) => (
              <div
                key={p.id}
                style={{
                  padding: "12px 18px",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  borderBottom: i < Math.min(9, prizeLog.length - 1) ? `1px solid rgba(212,165,72,0.12)` : "none",
                }}
              >
                <div>
                  <div style={{ fontSize: 15, color: COLORS.cream }}>{p.name}</div>
                  <div style={{ fontSize: 12, color: COLORS.creamDim, marginTop: 2 }}>
                    {new Date(p.at).toLocaleString([], {
                      month: "short",
                      day: "numeric",
                      hour: "numeric",
                      minute: "2-digit",
                    })}
                  </div>
                </div>
                <div className="mono" style={{ fontSize: 14, color: COLORS.gold }}>
                  −{p.cost.toLocaleString()}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// ============================================================
// STATS VIEW
// ============================================================
function StatsView({
  totalStudied,
  bySubject,
  sessions,
  credits,
  tokens,
  prizeLog,
  exportData,
  importData,
  resetData,
  addPastSession,
  editSession,
  deleteSession,
}) {
  const maxVal = Math.max(1, ...Object.values(bySubject));
  const totalEarnedFromStudy = Math.floor(totalStudied / 60);
  const totalSpent = prizeLog.reduce((a, p) => a + p.cost, 0);

  const [showImport, setShowImport] = useState(false);
  const [importText, setImportText] = useState("");
  const [importError, setImportError] = useState("");
  const [resetStage, setResetStage] = useState(0);
  const resetTimerRef = useRef(null);

  const handleImport = () => {
    try {
      const data = JSON.parse(importText);
      if (typeof data.credits !== "number" || !Array.isArray(data.sessions)) {
        setImportError("That does not look like a valid backup file.");
        return;
      }
      importData(data);
      setImportText("");
      setImportError("");
      setShowImport(false);
    } catch (e) {
      setImportError("Could not parse JSON: " + e.message);
    }
  };

  const handleReset = () => {
    if (resetStage === 0) {
      setResetStage(1);
      resetTimerRef.current = setTimeout(() => setResetStage(0), 3000);
    } else {
      if (resetTimerRef.current) clearTimeout(resetTimerRef.current);
      resetData();
      setResetStage(0);
    }
  };

  return (
    <div>
      <SectionTitle>The Ledger</SectionTitle>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: 12,
          marginBottom: 40,
        }}
      >
        <StatCard label="Total studied" value={fmtHoursMin(totalStudied)} />
        <StatCard label="Sessions" value={sessions.length.toLocaleString()} />
        <StatCard label="Earned from study" value={totalEarnedFromStudy.toLocaleString()} />
        <StatCard label="Tokens spent" value={totalSpent.toLocaleString()} />
        <StatCard label="Credits" value={credits.toLocaleString()} accent />
        <StatCard label="Tokens" value={tokens.toLocaleString()} accent />
      </div>

      <SectionTitle>By Subject</SectionTitle>
      <div className="panel" style={{ padding: 20, marginBottom: 40 }}>
        {SUBJECTS.map((s) => {
          const val = bySubject[s] || 0;
          const pct = maxVal > 0 ? (val / maxVal) * 100 : 0;
          return (
            <div key={s} style={{ marginBottom: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
                <span style={{ color: COLORS.cream }}>{s}</span>
                <span className="mono" style={{ color: COLORS.creamDim }}>
                  {fmtHoursMin(val)}
                </span>
              </div>
              <div style={{ height: 6, background: "rgba(0,0,0,0.4)", borderRadius: 3, overflow: "hidden" }}>
                <div
                  style={{
                    height: "100%",
                    width: `${pct}%`,
                    background: val > 0 ? `linear-gradient(90deg, ${COLORS.goldDim}, ${COLORS.gold})` : "transparent",
                    transition: "width 0.4s",
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>

      <SectionTitle>Add a past session</SectionTitle>
      <div style={{ fontSize: 12, color: COLORS.creamDim, marginBottom: 10 }}>
        Studied away from the device? Log it here. Earned credits are minutes of study, same as a live session.
      </div>
      <AddPastSessionForm onAdd={addPastSession} />

      {sessions.length > 0 && (
        <>
          <SectionTitle>All Sessions</SectionTitle>
          <div style={{ fontSize: 12, color: COLORS.creamDim, marginBottom: 10 }}>
            Tap the pencil to edit a session's subject or duration. Editing adjusts your credit balance by the
            difference.
          </div>
          <div className="panel" style={{ padding: 0, maxHeight: 500, overflowY: "auto", marginBottom: 40 }}>
            {sessions.map((s, i) => (
              <SessionRow
                key={s.id}
                session={s}
                isLast={i === sessions.length - 1}
                onEdit={editSession}
                onDelete={deleteSession}
              />
            ))}
          </div>
        </>
      )}

      <SectionTitle>Data</SectionTitle>
      <div className="panel" style={{ padding: 20 }}>
        <div style={{ fontSize: 13, color: COLORS.creamDim, marginBottom: 16, lineHeight: 1.6 }}>
          Your state is saved automatically to this artifact's persistent storage. It survives reloads, tab closes, and
          returning days later. Export a JSON backup any time you want a copy outside of Claude.
        </div>

        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button className="btn" onClick={exportData}>
            Export backup
          </button>
          <button
            className="btn"
            onClick={() => {
              setShowImport(!showImport);
              setImportError("");
            }}
          >
            {showImport ? "Cancel import" : "Import backup"}
          </button>
          <button className="btn btn-danger" onClick={handleReset}>
            {resetStage === 1 ? "Click again to confirm reset" : "Reset all data"}
          </button>
        </div>

        {showImport && (
          <div style={{ marginTop: 16 }}>
            <textarea
              value={importText}
              onChange={(e) => {
                setImportText(e.target.value);
                setImportError("");
              }}
              placeholder="Paste the contents of a backup JSON file here"
              style={{
                width: "100%",
                minHeight: 140,
                background: "rgba(0,0,0,0.3)",
                border: `1px solid rgba(212,165,72,0.4)`,
                color: COLORS.cream,
                padding: 10,
                fontFamily: "monospace",
                fontSize: 11,
                borderRadius: 2,
                resize: "vertical",
              }}
            />
            {importError && <div style={{ color: COLORS.red, fontSize: 12, marginTop: 6 }}>{importError}</div>}
            <div style={{ fontSize: 11, color: COLORS.creamDim, marginTop: 8, marginBottom: 8 }}>
              Loading a backup replaces all current data. Your existing credits, sessions, and prizes will be lost.
            </div>
            <button className="btn btn-primary" onClick={handleImport} disabled={!importText.trim()}>
              Load backup data
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// SHARED COMPONENTS
// ============================================================

// Returns a "YYYY-MM-DDTHH:MM" string in the user's local timezone, suitable
// for the value of an <input type="datetime-local">. The native Date.toISO
// methods return UTC, which would shift the picker by the browser's offset.
function localDatetimeInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function AddPastSessionForm({ onAdd }) {
  const [subject, setSubject] = useState(SUBJECTS[0]);
  const [hours, setHours] = useState(0);
  const [minutes, setMinutes] = useState(30);
  const [endedAt, setEndedAt] = useState(() => localDatetimeInputValue(new Date()));

  const seconds = Math.max(0, parseInt(hours) || 0) * 3600 + Math.max(0, Math.min(59, parseInt(minutes) || 0)) * 60;
  // Browsers parse "YYYY-MM-DDTHH:MM" as local time, which is what the picker
  // shows the user — no manual TZ adjustment needed.
  const endedAtMs = new Date(endedAt).getTime();
  const canAdd = subject && seconds > 0 && Number.isFinite(endedAtMs);

  const handleAdd = () => {
    if (!canAdd) return;
    onAdd(subject, seconds, endedAtMs);
    setHours(0);
    setMinutes(30);
    setEndedAt(localDatetimeInputValue(new Date()));
  };

  const minutesEarned = Math.floor(seconds / 60);

  return (
    <div className="panel" style={{ padding: 18, marginBottom: 32 }}>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(140px, 1.4fr) minmax(180px, 1fr) minmax(200px, 1.2fr) auto",
          gap: 10,
          alignItems: "end",
        }}
      >
        <div>
          <div
            style={{
              fontSize: 10,
              color: COLORS.creamDim,
              letterSpacing: "0.2em",
              textTransform: "uppercase",
              marginBottom: 4,
            }}
          >
            Subject
          </div>
          <select value={subject} onChange={(e) => setSubject(e.target.value)} style={{ width: "100%" }}>
            {SUBJECTS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div>
          <div
            style={{
              fontSize: 10,
              color: COLORS.creamDim,
              letterSpacing: "0.2em",
              textTransform: "uppercase",
              marginBottom: 4,
            }}
          >
            Duration
          </div>
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            <input
              type="number"
              value={hours}
              onChange={(e) => setHours(e.target.value)}
              min="0"
              style={{ width: 64 }}
            />
            <span style={{ fontSize: 12, color: COLORS.creamDim }}>h</span>
            <input
              type="number"
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              min="0"
              max="59"
              style={{ width: 64 }}
            />
            <span style={{ fontSize: 12, color: COLORS.creamDim }}>m</span>
          </div>
        </div>
        <div>
          <div
            style={{
              fontSize: 10,
              color: COLORS.creamDim,
              letterSpacing: "0.2em",
              textTransform: "uppercase",
              marginBottom: 4,
            }}
          >
            Ended at
          </div>
          <input
            type="datetime-local"
            value={endedAt}
            onChange={(e) => setEndedAt(e.target.value)}
            style={{ width: "100%" }}
          />
        </div>
        <button className="btn btn-primary" onClick={handleAdd} disabled={!canAdd}>
          Add session
        </button>
      </div>
      <div style={{ fontSize: 11, color: COLORS.creamDim, marginTop: 10 }}>
        {canAdd ? (
          <>
            Will log {fmtHoursMin(seconds)} of {subject} ending{" "}
            {new Date(endedAtMs).toLocaleString([], {
              month: "short",
              day: "numeric",
              hour: "numeric",
              minute: "2-digit",
            })}
            . Earns <strong style={{ color: COLORS.gold }}>+{minutesEarned} credits</strong>.
          </>
        ) : (
          "Pick a subject, duration, and end time."
        )}
      </div>
    </div>
  );
}

function SessionRow({ session, isLast, onEdit, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [subject, setSubject] = useState(session.subject);
  const [hours, setHours] = useState(Math.floor(session.seconds / 3600));
  const [minutes, setMinutes] = useState(Math.floor((session.seconds % 3600) / 60));

  const startEdit = () => {
    setSubject(session.subject);
    setHours(Math.floor(session.seconds / 3600));
    setMinutes(Math.floor((session.seconds % 3600) / 60));
    setEditing(true);
    setConfirmDel(false);
  };

  const saveEdit = () => {
    const h = Math.max(0, parseInt(hours) || 0);
    const m = Math.max(0, Math.min(59, parseInt(minutes) || 0));
    const newSeconds = h * 3600 + m * 60;
    onEdit(session.id, { subject, seconds: newSeconds });
    setEditing(false);
  };

  const handleDelete = () => {
    if (confirmDel) {
      onDelete(session.id);
    } else {
      setConfirmDel(true);
      setTimeout(() => setConfirmDel(false), 3000);
    }
  };

  const baseStyle = {
    padding: "10px 18px",
    borderBottom: isLast ? "none" : `1px solid rgba(212,165,72,0.08)`,
    fontSize: 13,
  };

  if (editing) {
    return (
      <div style={{ ...baseStyle, background: "rgba(212,165,72,0.05)" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            style={{ flex: "1 1 140px", minWidth: 120 }}
          >
            {SUBJECTS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            <input
              type="number"
              value={hours}
              onChange={(e) => setHours(e.target.value)}
              min="0"
              style={{ width: 56 }}
            />
            <span style={{ fontSize: 12, color: COLORS.creamDim }}>h</span>
            <input
              type="number"
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              min="0"
              max="59"
              style={{ width: 56 }}
            />
            <span style={{ fontSize: 12, color: COLORS.creamDim }}>m</span>
          </div>
          <div style={{ display: "flex", gap: 4 }}>
            <button className="btn btn-primary" style={{ padding: "6px 12px", fontSize: 11 }} onClick={saveEdit}>
              Save
            </button>
            <button className="btn" style={{ padding: "6px 12px", fontSize: 11 }} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
        <div style={{ fontSize: 11, color: COLORS.creamDim, marginTop: 6 }}>
          Current: {fmtHoursMin(session.seconds)} ({Math.floor(session.seconds / 60)} cr). Changing duration will adjust
          your credit balance by the difference.
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        ...baseStyle,
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div style={{ minWidth: 0, flex: 1 }}>
        <span style={{ color: COLORS.cream }}>{session.subject}</span>
        <span style={{ color: COLORS.creamDim, marginLeft: 12, fontSize: 12 }}>
          {new Date(session.endedAt).toLocaleString([], {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}
        </span>
      </div>
      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <span className="mono" style={{ color: COLORS.cream }}>
          {fmtHoursMin(session.seconds)}
        </span>
        <span className="mono" style={{ color: COLORS.gold, minWidth: 56, textAlign: "right", fontSize: 12 }}>
          +{Math.floor(session.seconds / 60)} cr
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            onClick={startEdit}
            title="Edit session"
            style={{
              width: 26,
              height: 26,
              padding: 0,
              background: "transparent",
              border: `1px solid rgba(212,165,72,0.3)`,
              color: COLORS.creamDim,
              cursor: "pointer",
              fontSize: 12,
              borderRadius: 2,
            }}
          >
            ✎
          </button>
          <button
            onClick={handleDelete}
            title={confirmDel ? "Click again to confirm" : "Delete session"}
            style={{
              width: confirmDel ? "auto" : 26,
              height: 26,
              padding: confirmDel ? "0 8px" : 0,
              background: confirmDel ? COLORS.red : "transparent",
              border: `1px solid ${confirmDel ? COLORS.red : "rgba(178,57,57,0.4)"}`,
              color: confirmDel ? COLORS.cream : COLORS.red,
              cursor: "pointer",
              fontSize: confirmDel ? 10 : 14,
              borderRadius: 2,
              letterSpacing: confirmDel ? "0.1em" : 0,
              textTransform: confirmDel ? "uppercase" : "none",
              fontWeight: confirmDel ? 600 : 400,
            }}
          >
            {confirmDel ? "Confirm" : "×"}
          </button>
        </div>
      </div>
    </div>
  );
}

function StatCard({ label, value, accent }) {
  return (
    <div
      className="deco-corners"
      style={{
        padding: "16px 20px",
        background: "rgba(0,0,0,0.3)",
        border: `1px solid ${accent ? COLORS.gold : "rgba(212,165,72,0.25)"}`,
      }}
    >
      <div
        style={{
          fontSize: 11,
          color: COLORS.creamDim,
          letterSpacing: "0.2em",
          textTransform: "uppercase",
          marginBottom: 6,
        }}
      >
        {label}
      </div>
      <div
        className="display-font mono"
        style={{
          fontSize: 24,
          fontWeight: 700,
          color: accent ? COLORS.gold : COLORS.cream,
        }}
      >
        {value}
      </div>
    </div>
  );
}

// Celebratory burst rendered absolutely inside a game panel when the player
// wins tokens. Particles fly out from the center and fall under gravity; a
// big "+N tokens" pops in the middle and a quick gold flash washes the panel.
// Mount under a `key={Date.now()}` so each new win replays the animation.
const WIN_PARTICLE_COUNT = 28;
const WIN_GLYPHS = ["◆", "★", "♦", "♠", "$", "✦", "♥"];

function WinBurst({ amount }) {
  const [done, setDone] = useState(false);

  useEffect(() => {
    const id = setTimeout(() => setDone(true), 2400);
    return () => clearTimeout(id);
  }, []);

  if (done) return null;

  const particles = Array.from({ length: WIN_PARTICLE_COUNT }, (_, i) => {
    const angle = (Math.PI * 2 * i) / WIN_PARTICLE_COUNT + (Math.random() - 0.5) * 0.4;
    const dist = 140 + Math.random() * 200;
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist - 90;
    const rot = (Math.random() - 0.5) * 720;
    const delay = Math.random() * 0.18;
    const dur = 1.6 + Math.random() * 0.5;
    const size = 18 + Math.floor(Math.random() * 18);
    const glyph = WIN_GLYPHS[Math.floor(Math.random() * WIN_GLYPHS.length)];
    const goldenTone = Math.random() > 0.35;
    return { dx, dy, rot, delay, dur, size, glyph, goldenTone };
  });

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        pointerEvents: "none",
        overflow: "hidden",
        zIndex: 30,
      }}
    >
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: `radial-gradient(circle at center, ${COLORS.goldBright}, transparent 70%)`,
          animation: "win-flash 0.9s ease-out forwards",
        }}
      />
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          width: 120,
          height: 120,
          marginLeft: -60,
          marginTop: -60,
          borderRadius: "50%",
          border: `3px solid ${COLORS.goldBright}`,
          boxShadow: `0 0 40px ${COLORS.goldBright}`,
          animation: "win-ring 1s ease-out forwards",
        }}
      />
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          width: 0,
          height: 0,
        }}
      >
        {particles.map((p, i) => (
          <span
            key={i}
            style={{
              position: "absolute",
              left: 0,
              top: 0,
              fontSize: p.size,
              fontFamily: "'Playfair Display', Georgia, serif",
              fontWeight: 700,
              color: p.goldenTone ? COLORS.goldBright : COLORS.rose,
              textShadow: `0 0 8px ${p.goldenTone ? COLORS.gold : COLORS.rose}`,
              animation: `win-particle ${p.dur}s cubic-bezier(0.2, 0.6, 0.4, 1) ${p.delay}s forwards`,
              willChange: "transform, opacity",
              "--dx": `${p.dx}px`,
              "--dy": `${p.dy}px`,
              "--rot": `${p.rot}deg`,
            }}
          >
            {p.glyph}
          </span>
        ))}
      </div>
      <div
        className="display-font"
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          fontSize: 56,
          fontWeight: 900,
          color: COLORS.goldBright,
          textShadow: `0 0 30px ${COLORS.goldBright}, 0 0 12px ${COLORS.goldBright}, 0 4px 8px rgba(0,0,0,0.6)`,
          letterSpacing: "0.05em",
          whiteSpace: "nowrap",
          animation: "win-text-pop 1.8s cubic-bezier(0.2, 0.7, 0.3, 1) forwards",
        }}
      >
        +{amount.toLocaleString()} <span style={{ fontSize: 24, color: COLORS.rose }}>tokens</span>
      </div>
    </div>
  );
}

function SectionTitle({ children, small }) {
  return (
    <div
      className="display-font"
      style={{
        fontSize: small ? 14 : 16,
        color: COLORS.gold,
        letterSpacing: "0.3em",
        textTransform: "uppercase",
        marginBottom: 14,
        fontWeight: 600,
        paddingBottom: 6,
        borderBottom: `1px solid rgba(212,165,72,0.2)`,
      }}
    >
      {children}
    </div>
  );
}
