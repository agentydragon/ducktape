import React, { useEffect, useState, useCallback } from "react";

import { COLORS, SectionTitle, fmtCredits, fmtHoursMin } from "./shared.jsx";
import { fetchAdminUsers, fetchAdminUserState, useSyncStatus } from "./sync.js";

export function AdminView({ addPrize, deletePrize, ownUsername }) {
  const [users, setUsers] = useState([]);
  const [selected, setSelected] = useState(null);
  const [targetState, setTargetState] = useState(null);
  const [error, setError] = useState(null);
  const [newName, setNewName] = useState("");
  const [newCost, setNewCost] = useState("");
  const syncStatus = useSyncStatus();
  const offline = syncStatus.kind === "offline";

  const refreshUsers = useCallback(async () => {
    try {
      const list = await fetchAdminUsers();
      setUsers(list);
      setError(null);
      // Default to the first non-self user; fall back to self.
      if (selected === null) {
        const other = list.find((u) => u !== ownUsername);
        setSelected(other ?? list[0] ?? null);
      }
    } catch (e) {
      setError(`Failed to list users: ${e.message}`);
    }
  }, [ownUsername, selected]);

  const refreshTargetState = useCallback(async () => {
    if (!selected) {
      setTargetState(null);
      return;
    }
    try {
      setTargetState(await fetchAdminUserState(selected));
      setError(null);
    } catch (e) {
      setError(`Failed to load state for ${selected}: ${e.message}`);
    }
  }, [selected]);

  useEffect(() => {
    refreshUsers();
  }, [refreshUsers]);

  useEffect(() => {
    refreshTargetState();
  }, [refreshTargetState]);

  const handleAdd = async () => {
    const cost = parseInt(newCost);
    if (!newName.trim() || !cost || cost <= 0 || !selected) return;
    await addPrize(newName.trim(), cost, selected);
    setNewName("");
    setNewCost("");
    refreshTargetState();
  };

  const handleDelete = async (prizeId) => {
    await deletePrize(prizeId, selected);
    refreshTargetState();
  };

  const prizes = targetState?.prizes ?? [];
  const balance = targetState?.balance ?? { credits_millis: 0, tokens: 0 };

  return (
    <div>
      <SectionTitle>Admin: manage prize catalogs</SectionTitle>

      {error && (
        <div
          style={{
            background: "rgba(180,40,40,0.25)",
            border: `1px solid ${COLORS.red}`,
            padding: "10px 14px",
            marginBottom: 16,
            color: COLORS.cream,
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}

      <div className="panel" style={{ padding: 16, marginBottom: 24, display: "flex", gap: 12, alignItems: "center" }}>
        <span style={{ color: COLORS.creamDim, fontSize: 12, letterSpacing: "0.1em", textTransform: "uppercase" }}>
          Managing prizes for
        </span>
        <select value={selected ?? ""} onChange={(e) => setSelected(e.target.value || null)}>
          {users.length === 0 && <option value="">(no users yet)</option>}
          {users.map((u) => (
            <option key={u} value={u}>
              {u}
              {u === ownUsername ? " (you)" : ""}
            </option>
          ))}
        </select>
        {selected && (
          <span style={{ color: COLORS.creamDim, fontSize: 12, marginLeft: 12 }}>
            Balance: <strong style={{ color: COLORS.gold }}>{fmtCredits(balance.credits_millis / 1000)}</strong> credits
            · <strong style={{ color: COLORS.rose }}>{balance.tokens.toLocaleString()}</strong> tokens
          </span>
        )}
      </div>

      <SectionTitle>Existing prizes</SectionTitle>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
          gap: 12,
          marginBottom: 32,
        }}
      >
        {prizes.length === 0 && (
          <div style={{ color: COLORS.creamDim, fontSize: 13 }}>No prizes in this user's catalog.</div>
        )}
        {prizes.map((p) => (
          <div
            key={p.id}
            className="deco-corners"
            style={{
              padding: 18,
              background: "rgba(0,0,0,0.3)",
              border: "1px solid rgba(212,165,72,0.25)",
              position: "relative",
            }}
          >
            <button
              onClick={() => handleDelete(p.id)}
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
            <div className="display-font" style={{ fontSize: 17, color: COLORS.cream, marginBottom: 8, minHeight: 44 }}>
              {p.name}
            </div>
            <div
              style={{
                fontSize: 12,
                color: COLORS.creamDim,
                letterSpacing: "0.15em",
                textTransform: "uppercase",
              }}
            >
              {p.cost.toLocaleString()} tokens · {fmtHoursMin(p.cost * 60)}
            </div>
          </div>
        ))}
      </div>

      <SectionTitle>Add a prize for {selected ?? "…"}</SectionTitle>
      <div className="panel" style={{ padding: 20 }}>
        <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr auto", gap: 10 }}>
          <input
            placeholder="Prize name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            disabled={!selected}
          />
          <input
            type="number"
            placeholder="Cost (tokens)"
            value={newCost}
            onChange={(e) => setNewCost(e.target.value)}
            disabled={!selected}
          />
          <button
            className="btn btn-primary"
            onClick={handleAdd}
            disabled={offline || !selected || !newName.trim() || !parseInt(newCost)}
          >
            Add
          </button>
        </div>
      </div>
    </div>
  );
}
