import { useState } from "react";

import { COLORS, SectionTitle, WagerInput, WagerPresets, WinBurst } from "./shared.jsx";

function PlayingCard({ card }) {
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
  const [hand, setHand] = useState(null);
  const [betInput, setBetInput] = useState(10);
  const [winBurst, setWinBurst] = useState(null);
  const [pending, setPending] = useState(null);

  const {
    hand_id: handId,
    phase = "betting",
    current_wager: wager,
    player_cards: playerHand = [],
    dealer_cards: dealerHand = [],
    player_value: playerValue,
    dealer_value: dealerValue,
    settlement: result,
  } = hand ?? {};

  const dealerPlaying = pending === "stand" || pending === "double";
  const ready = !offline && pending === null;
  const canDeal = ready && phase === "betting" && betInput > 0 && betInput <= credits;
  const canStand = ready && phase === "playing" && handId;
  const canHit = canStand && playerValue < 21;
  const canDouble = canStand && playerHand.length === 2 && credits >= wager;

  const play = async (action, request) => {
    setPending(action);
    try {
      const nextHand = await request();
      setHand(nextHand);
      const payout = nextHand.settlement?.payout_tokens;
      if (payout > 0) setWinBurst({ key: Date.now(), amount: payout });
    } catch (e) {
      console.debug(`[Casino] blackjack ${action} failed:`, e.message ?? String(e));
    } finally {
      setPending(null);
    }
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
        <div role="group" aria-label="Dealer hand" style={{ marginBottom: 28 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div
              className="display-font"
              style={{ fontSize: 13, letterSpacing: "0.25em", textTransform: "uppercase", color: COLORS.creamDim }}
            >
              Dealer
            </div>
            <div className="mono" style={{ fontSize: 15, color: COLORS.cream }}>
              {dealerHand.length > 0 ? (phase === "playing" ? `${dealerValue} + ?` : dealerValue) : ""}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, minHeight: 92, flexWrap: "wrap" }}>
            {dealerHand.map((c, i) => (
              <PlayingCard key={`${c.rank}-${c.suit}-${i}`} card={c} />
            ))}
          </div>
        </div>

        {/* Player */}
        <div role="group" aria-label="Player hand">
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
            role="status"
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
            {result.payout_tokens > 0 && (
              <div style={{ fontSize: 14, color: COLORS.rose, marginTop: 4 }}>+{result.payout_tokens} tokens</div>
            )}
          </div>
        )}
      </div>

      <div>
        <div className="panel" style={{ padding: 18, marginBottom: 12 }}>
          <SectionTitle small>Wager</SectionTitle>

          {phase === "betting" ? (
            <>
              <WagerInput
                credits={credits}
                value={betInput}
                onChange={setBetInput}
                style={{ width: "100%", marginBottom: 8 }}
              />
              <WagerPresets credits={credits} onChange={setBetInput} style={{ marginBottom: 12 }} />
              <button
                className="btn btn-primary"
                style={{ width: "100%", padding: 12 }}
                onClick={() => play("deal", () => blackjackDeal(betInput))}
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

              {phase === "playing" && !dealerPlaying && (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 6 }}>
                  <button
                    className="btn btn-primary"
                    style={{ padding: 10 }}
                    onClick={() => play("hit", () => blackjackHit(handId))}
                    disabled={!canHit}
                  >
                    Hit
                  </button>
                  <button
                    className="btn btn-primary"
                    style={{ padding: 10 }}
                    onClick={() => play("stand", () => blackjackStand(handId))}
                    disabled={!canStand}
                  >
                    Stand
                  </button>
                  <button
                    className="btn"
                    style={{ padding: 10, gridColumn: "span 2" }}
                    onClick={() => play("double", () => blackjackDouble(handId))}
                    disabled={!canDouble}
                  >
                    Double {credits < wager ? "(not enough credits)" : `(+${wager})`}
                  </button>
                </div>
              )}

              {dealerPlaying && (
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
                <button
                  className="btn btn-primary"
                  style={{ width: "100%", padding: 12 }}
                  onClick={() => setHand(null)}
                >
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
