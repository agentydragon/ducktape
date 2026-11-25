import { useState, useEffect, useRef } from "react";

import gameRules from "../game_rules.json";
import { COLORS, SectionTitle, WagerInput, WagerPresets, WinBurst } from "./shared.jsx";

const { slot_symbols: symbols, slot_pair_multiplier: pairMultiplier } = gameRules;

function SlotReel({ target, index, spinning }) {
  const [displayed, setDisplayed] = useState(target);
  const [blurred, setBlurred] = useState(false);
  const targetRef = useRef(target);
  targetRef.current = target;

  useEffect(() => {
    if (!spinning) return;
    setBlurred(true);
    const interval = setInterval(() => {
      setDisplayed(symbols[Math.floor(Math.random() * symbols.length)]);
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

export function Slots({ offline, credits, spinSlots }) {
  const [bet, setBet] = useState(5);
  const [targets, setTargets] = useState(symbols.slice(2, 5));
  const [spinning, setSpinning] = useState(false);
  const [lastResult, setLastResult] = useState(null);
  const [winBurst, setWinBurst] = useState(null);

  const canSpin = !offline && !spinning && bet > 0 && bet <= credits;

  const spin = async () => {
    if (!canSpin) return;
    const wager = bet;
    let serverResult;
    try {
      serverResult = await spinSlots(wager);
    } catch (e) {
      console.debug("[Casino] slots spin failed:", e.message ?? String(e));
      return;
    }
    setSpinning(true);
    setLastResult(null);

    const picks = serverResult.symbols.map((id) => symbols.find((s) => s.id === id) || symbols[0]);
    setTargets(picks);

    setTimeout(() => {
      const grossPayout = serverResult.payout_tokens;
      const label = serverResult.label;
      if (grossPayout > 0) {
        setWinBurst({ key: Date.now(), amount: grossPayout });
      }
      setLastResult({ picks, payout: grossPayout, label });
      setSpinning(false);
    }, 4000);
  };

  return (
    <div className="game-grid" style={{ "--sidebar-min": "240px", "--sidebar-max": "320px" }}>
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
          <WagerInput
            credits={credits}
            value={bet}
            onChange={setBet}
            disabled={spinning}
            style={{ width: "100%", marginBottom: 10 }}
          />
          <WagerPresets
            credits={credits}
            onChange={setBet}
            disabled={spinning}
            amounts={[1, 5, 10, 25, 50]}
            style={{ marginBottom: 16 }}
          />

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
          {symbols.map((s) => (
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
              {pairMultiplier}×
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
