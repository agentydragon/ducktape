import React, { useState } from "react";

import { COLORS, SectionTitle, fmtCredits, fmtHoursMin } from "./shared.jsx";

export function PrizesView({
  offline,
  isAdmin,
  credits,
  tokens,
  prizes,
  prizeLog,
  redeem,
  addPrize,
  deletePrize,
  convertToTokens,
}) {
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
              {fmtCredits(credits)}
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
            disabled={offline || !parseInt(convertAmount) || parseInt(convertAmount) > credits}
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
          {Math.floor(credits) > 0 && (
            <button
              onClick={() => setConvertAmount(String(Math.floor(credits)))}
              style={{
                padding: "4px 10px",
                fontSize: 11,
                background: "transparent",
                color: pinkText,
                border: `1px solid ${COLORS.wine}`,
                cursor: "pointer",
              }}
            >
              All ({Math.floor(credits)})
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
              {isAdmin && (
                <button
                  onClick={() => deletePrize(p.id)}
                  disabled={offline}
                  style={{
                    position: "absolute",
                    top: 6,
                    right: 6,
                    background: "transparent",
                    border: "none",
                    color: offline ? "rgba(201,188,154,0.3)" : COLORS.creamDim,
                    cursor: "pointer",
                    fontSize: 16,
                    padding: 4,
                    lineHeight: 1,
                  }}
                  title="Delete"
                >
                  ×
                </button>
              )}
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
                    disabled={offline}
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
                  disabled={offline || !canAfford}
                  onClick={() => setConfirming(p.id)}
                >
                  {canAfford ? "Redeem" : `Need ${p.cost - tokens} more tokens`}
                </button>
              )}
            </div>
          );
        })}
      </div>

      {isAdmin && (
        <>
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
              <button
                className="btn btn-primary"
                onClick={handleAdd}
                disabled={offline || !newName.trim() || !parseInt(newCost)}
              >
                Add
              </button>
            </div>
            <div style={{ fontSize: 12, color: COLORS.creamDim, marginTop: 10 }}>
              Prize cost is in tokens. Since 1 credit = 1 minute of study and 1 credit converts to 1 token, a 120-token
              prize costs 2 hours of study.
            </div>
          </div>
        </>
      )}

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
