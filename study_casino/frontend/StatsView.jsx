import React, { useState, useRef, useCallback, useEffect } from "react";

import {
  COLORS,
  fmtCredits,
  fmtHoursMin,
  mapSessionRead,
  AdminUserPicker,
  ErrorBanner,
  SectionTitle,
  StatCard,
  AddPastSessionForm,
  SessionRow,
  StudyHabitsSections,
} from "./shared.jsx";
import { fetchAdminUserState } from "./sync.js";

export function StatsView({
  offline,
  totalStudied,
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
  isAdmin,
}) {
  const totalEarnedFromStudy = Math.floor(totalStudied / 60);
  const totalSpent = prizeLog.reduce((a, p) => a + p.cost, 0);

  const [showImport, setShowImport] = useState(false);
  const [importText, setImportText] = useState("");
  const [importError, setImportError] = useState("");
  const [resetStage, setResetStage] = useState(0);
  const resetTimerRef = useRef(null);

  const [viewingUser, setViewingUser] = useState(() =>
    isAdmin ? (new URLSearchParams(window.location.search).get("user") ?? "") : ""
  );
  const [targetSessions, setTargetSessions] = useState([]);
  const [targetError, setTargetError] = useState(null);
  const viewingOther = isAdmin && viewingUser.trim().length > 0;

  const refreshTarget = useCallback(async () => {
    const user = viewingUser.trim();
    if (!isAdmin || !user) {
      setTargetSessions([]);
      setTargetError(null);
      return;
    }
    try {
      const state = await fetchAdminUserState(user);
      setTargetSessions(state.sessions.map(mapSessionRead));
      setTargetError(null);
    } catch (e) {
      setTargetError(`Failed to load sessions for ${user}: ${e.message}`);
      setTargetSessions([]);
    }
  }, [isAdmin, viewingUser]);

  useEffect(() => {
    refreshTarget();
  }, [refreshTarget]);

  const habitsSessions = viewingOther ? targetSessions : sessions;

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
        <StatCard label="Credits" value={fmtCredits(credits)} accent />
        <StatCard label="Tokens" value={tokens.toLocaleString()} accent />
      </div>

      {isAdmin && (
        <AdminUserPicker
          label="Viewing habits for"
          value={viewingUser}
          onChange={setViewingUser}
          onReload={refreshTarget}
        />
      )}

      {viewingOther && (
        <div style={{ fontSize: 11, color: COLORS.creamDim, marginBottom: 16 }}>
          Showing the "By Subject" and "Over Time" graphs below for{" "}
          <span style={{ color: COLORS.gold }}>{viewingUser.trim()}</span>. The ledger, session editor, and data
          controls above and below still apply to your own account.
        </div>
      )}

      {targetError && <ErrorBanner>{targetError}</ErrorBanner>}

      <StudyHabitsSections sessions={habitsSessions} />

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
          Your state is saved automatically on the server and synced to every device you sign in from. Export a JSON
          backup any time you want an offline copy.
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
