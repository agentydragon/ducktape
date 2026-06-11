import React, { useState, useRef } from "react";

import { COLORS, SUBJECTS, fmtHoursMin, SectionTitle, StatCard, AddPastSessionForm, SessionRow } from "./shared.jsx";

export function StatsView({
  offline,
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
      <AddPastSessionForm offline={offline} onAdd={addPastSession} />

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
                offline={offline}
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
            disabled={offline}
            onClick={() => {
              setShowImport(!showImport);
              setImportError("");
            }}
          >
            {showImport ? "Cancel import" : "Import backup"}
          </button>
          <button className="btn btn-danger" onClick={handleReset} disabled={offline}>
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
            <button className="btn btn-primary" onClick={handleImport} disabled={offline || !importText.trim()}>
              Load backup data
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
