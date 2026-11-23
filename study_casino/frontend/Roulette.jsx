import { useState } from "react";

import gameRules from "../game_rules.json";
import { COLORS, SectionTitle, WagerInput, WagerPresets, WinBurst } from "./shared.jsx";

const RED = new Set(gameRules.red);
const POCKET_ANGLE = 360 / gameRules.wheel.length;

// Fill for a wheel pocket or a result chip: green zero, otherwise red or black.
function pocketFill(n) {
  if (n === 0) return "#1e6e3e";
  return RED.has(n) ? COLORS.red : COLORS.black;
}

// Module-level (not defined inside Roulette's render) so React keeps the
// mounted <button> across re-renders instead of remounting it on every
// bet-amount keystroke.
function BetTypeBtn({ value, betType, setBetType, spinning, children, size = "md" }) {
  return (
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
}

export function Roulette({ offline, credits, spinRoulette }) {
  const [betAmount, setBetAmount] = useState(10);
  const [betType, setBetType] = useState("red");
  const [betNumber, setBetNumber] = useState(7);
  const [spinning, setSpinning] = useState(false);
  const [result, setResult] = useState(null);
  const [rotation, setRotation] = useState(0);
  const [history, setHistory] = useState([]);
  const [winBurst, setWinBurst] = useState(null);

  const canSpin = !offline && !spinning && betAmount > 0 && betAmount <= credits;

  const spin = async () => {
    if (!canSpin) return;
    const wager = betAmount;
    const betTypeSnapshot = betType;
    const betNumberSnapshot = betNumber;
    let serverResult;
    try {
      serverResult = await spinRoulette({
        wagerCredits: wager,
        betType: betTypeSnapshot,
        betNumber: betNumberSnapshot,
      });
    } catch (e) {
      console.debug("[Casino] roulette spin failed:", e.message ?? String(e));
      return;
    }
    const pickedIdx = serverResult.result_index;
    const picked = serverResult.result_number;

    setSpinning(true);
    setResult(null);

    const targetAngle = -(pickedIdx * POCKET_ANGLE);
    const fullSpins = 6 + Math.floor(Math.random() * 3);
    const finalRotation = rotation - fullSpins * 360 + (targetAngle - (rotation % 360));
    setRotation(finalRotation);

    setTimeout(() => {
      const grossPayout = serverResult.payout_tokens;
      if (grossPayout > 0) {
        setWinBurst({ key: Date.now(), amount: grossPayout });
      }
      setResult({ number: picked, won: serverResult.won, payout: grossPayout });
      setHistory((h) => [{ number: picked, won: serverResult.won }, ...h].slice(0, 10));
      setSpinning(false);
    }, 4200);
  };

  const betBtnProps = { betType, setBetType, spinning };

  return (
    <div className="game-grid" style={{ "--sidebar-min": "280px", "--sidebar-max": "360px" }}>
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
            {gameRules.wheel.map((num, i) => {
              const startAngle = i * POCKET_ANGLE - 90 - POCKET_ANGLE / 2;
              const endAngle = startAngle + POCKET_ANGLE;
              const sRad = (startAngle * Math.PI) / 180;
              const eRad = (endAngle * Math.PI) / 180;
              const r = 130;
              const x1 = Math.cos(sRad) * r,
                y1 = Math.sin(sRad) * r;
              const x2 = Math.cos(eRad) * r,
                y2 = Math.sin(eRad) * r;
              const textAngle = i * POCKET_ANGLE - 90;
              const tx = Math.cos((textAngle * Math.PI) / 180) * 108;
              const ty = Math.sin((textAngle * Math.PI) / 180) * 108;
              return (
                <g key={i}>
                  <path
                    d={`M 0 0 L ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2} Z`}
                    fill={pocketFill(num)}
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
                background: pocketFill(result.number),
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
                  background: pocketFill(h.number),
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
            <WagerInput
              credits={credits}
              value={betAmount}
              onChange={setBetAmount}
              disabled={spinning}
              style={{ flex: 1 }}
            />
          </div>
          <WagerPresets credits={credits} onChange={setBetAmount} disabled={spinning} style={{ marginTop: 6 }}>
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
          </WagerPresets>
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
            Bet type <span style={{ color: COLORS.gold }}>· pays {gameRules.roulette_multipliers[betType] - 1}:1</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
            <BetTypeBtn {...betBtnProps} value="red">
              <span style={{ color: betType === "red" ? COLORS.feltDeep : COLORS.red }}>●</span> Red
            </BetTypeBtn>
            <BetTypeBtn {...betBtnProps} value="black">
              <span>●</span> Black
            </BetTypeBtn>
            <BetTypeBtn {...betBtnProps} value="odd">
              Odd
            </BetTypeBtn>
            <BetTypeBtn {...betBtnProps} value="even">
              Even
            </BetTypeBtn>
            <BetTypeBtn {...betBtnProps} value="low">
              1–18
            </BetTypeBtn>
            <BetTypeBtn {...betBtnProps} value="high">
              19–36
            </BetTypeBtn>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4, marginTop: 4 }}>
            <BetTypeBtn {...betBtnProps} value="dozen1" size="sm">
              1st 12
            </BetTypeBtn>
            <BetTypeBtn {...betBtnProps} value="dozen2" size="sm">
              2nd 12
            </BetTypeBtn>
            <BetTypeBtn {...betBtnProps} value="dozen3" size="sm">
              3rd 12
            </BetTypeBtn>
          </div>
          <div style={{ marginTop: 8 }}>
            <BetTypeBtn {...betBtnProps} value="number">
              Single Number ({gameRules.roulette_multipliers.number - 1}:1)
            </BetTypeBtn>
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
