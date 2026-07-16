import React from "react";

import { COLORS, SectionTitle } from "./shared.jsx";

// Blocking "what's new" overlay shown while the server reports unacked
// changelog entries; "Got it" advances the per-user cursor server-side.
export function ChangelogModal({ entries, onAck }) {
  if (!entries.length) return null;
  const latestId = entries[entries.length - 1].id;
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 200,
        background: "rgba(31,10,16,0.85)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        className="deco-corners"
        style={{
          maxWidth: 640,
          maxHeight: "80vh",
          overflowY: "auto",
          background: COLORS.feltDark,
          border: `1px solid ${COLORS.gold}`,
          padding: "28px 32px",
        }}
      >
        <SectionTitle>What's new</SectionTitle>
        {entries.map((entry) => (
          <div key={entry.id} style={{ marginBottom: 20 }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
              <div className="display-font" style={{ fontSize: 18, color: COLORS.goldBright, fontWeight: 600 }}>
                {entry.title}
              </div>
              <div style={{ fontSize: 11, color: COLORS.creamDim, letterSpacing: "0.1em" }}>{entry.date}</div>
            </div>
            <ul style={{ margin: "10px 0 0", paddingLeft: 20, color: COLORS.cream, fontSize: 13, lineHeight: 1.7 }}>
              {entry.items.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          </div>
        ))}
        <div style={{ textAlign: "center", marginTop: 24 }}>
          <button className="btn btn-primary" onClick={() => onAck(latestId)}>
            Got it
          </button>
        </div>
      </div>
    </div>
  );
}
