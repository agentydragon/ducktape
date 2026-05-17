import React, { useState, useMemo } from "react";

import { COLORS, SectionTitle, WinBurst } from "./shared.jsx";

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

export function Blackjack({ offline, credits, blackjackDeal, blackjackHit, blackjackStand, blackjackDouble }) {
  const [handId, setHandId] = useState(null);
  const [playerHand, setPlayerHand] = useState([]);
  const [dealerHand, setDealerHand] = useState([]);
  const [phase, setPhase] = useState("betting");
  const [betInput, setBetInput] = useState(10);
  const [wager, setWager] = useState(0);
  const [result, setResult] = useState(null);
  const [holeHidden, setHoleHidden] = useState(true);
  const [winBurst, setWinBurst] = useState(null);
  const [pending, setPending] = useState(false);

  const playerValue = useMemo(() => handValue(playerHand), [playerHand]);
  const dealerValue = useMemo(() => handValue(dealerHand), [dealerHand]);
  const dealerVisibleValue = useMemo(() => {
    if (!holeHidden || dealerHand.length < 2) return handValue(dealerHand);
    return handValue([dealerHand[0]]);
  }, [dealerHand, holeHidden]);

  const canDeal = !offline && phase === "betting" && !pending && betInput > 0 && betInput <= credits;
  const canHit = !offline && phase === "playing" && !pending && handId && playerValue < 21;
  const canStand = !offline && phase === "playing" && !pending && handId;
  const canDouble =
    !offline && phase === "playing" && !pending && handId && playerHand.length === 2 && credits >= wager;

  const applyServerHand = (state) => {
    setHandId(state.hand_id);
    setPlayerHand(state.player_cards || []);
    setDealerHand(state.dealer_cards || []);
    setWager(state.current_wager || 0);
    setHoleHidden(!!state.hole_hidden);
    if (state.phase === "done" && state.settlement) {
      const payout = state.settlement.payout_tokens || 0;
      setResult({ outcome: state.settlement.outcome, payout, text: state.settlement.text });
      if (payout > 0) setWinBurst({ key: Date.now(), amount: payout });
      setPhase("done");
    } else {
      setResult(null);
      setPhase("playing");
    }
  };

  const deal = async () => {
    if (!canDeal) return;
    setPending(true);
    try {
      applyServerHand(await blackjackDeal(betInput));
    } catch (e) {
      console.debug("[Casino] blackjack deal failed:", e.message ?? String(e));
    } finally {
      setPending(false);
    }
  };

  const hit = async () => {
    if (!canHit) return;
    setPending(true);
    try {
      applyServerHand(await blackjackHit(handId));
    } catch (e) {
      console.debug("[Casino] blackjack hit failed:", e.message ?? String(e));
    } finally {
      setPending(false);
    }
  };

  const stand = async () => {
    if (!canStand) return;
    setPhase("dealer");
    setPending(true);
    try {
      applyServerHand(await blackjackStand(handId));
    } catch (e) {
      console.debug("[Casino] blackjack stand failed:", e.message ?? String(e));
      setPhase("playing");
    } finally {
      setPending(false);
    }
  };

  const doubleDown = async () => {
    if (!canDouble) return;
    setPhase("dealer");
    setPending(true);
    try {
      applyServerHand(await blackjackDouble(handId));
    } catch (e) {
      console.debug("[Casino] blackjack double failed:", e.message ?? String(e));
      setPhase("playing");
    } finally {
      setPending(false);
    }
  };

  const newHand = () => {
    setPlayerHand([]);
    setDealerHand([]);
    setResult(null);
    setWager(0);
    setHoleHidden(true);
    setHandId(null);
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
    <div className="game-grid" style={{ "--sidebar-min": "260px", "--sidebar-max": "340px" }}>
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
              <PlayingCard key={`${c.rank}-${c.suit}-${i}`} card={c} hidden={i === 1 && holeHidden} />
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
            {playerHand.map((c, i) => (
              <PlayingCard key={`${c.rank}-${c.suit}-${i}`} card={c} />
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
            Server-owned shoe
          </div>
        </div>
      </div>
    </div>
  );
}
