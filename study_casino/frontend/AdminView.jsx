import React, { useEffect, useState, useCallback } from "react";

import { COLORS, ErrorBanner, SectionTitle, fmtCredits, fmtHoursMin } from "./shared.jsx";
import { fetchAdminUsers, fetchAdminUserState, useSyncStatus } from "./sync.js";

export function AdminView({ addPrize, deletePrize, editPrize, ownUsername }) {
  const [users, setUsers] = useState([]);
  const [selected, setSelected] = useState(null);
  const [targetState, setTargetState] = useState(null);
  const [error, setError] = useState(null);
  const [newName, setNewName] = useState("");
  const [newCost, setNewCost] = useState("");
  const [editingPrizeId, setEditingPrizeId] = useState(null);
  const [editingName, setEditingName] = useState("");
  const [editingCost, setEditingCost] = useState("");
  const [savingPrizeId, setSavingPrizeId] = useState(null);
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
    if (editingPrizeId === prizeId) {
      setEditingPrizeId(null);
      setEditingName("");
      setEditingCost("");
    }
    await deletePrize(prizeId, selected);
    refreshTargetState();
  };

  const startEditing = (prize) => {
    setEditingPrizeId(prize.id);
    setEditingName(prize.name);
    setEditingCost(String(prize.cost));
    setError(null);
  };

  const cancelEditing = () => {
    setEditingPrizeId(null);
    setEditingName("");
    setEditingCost("");
  };

  const handleEdit = async (prize) => {
    const name = editingName.trim();
    const cost = Number(editingCost);
    if (!name || !Number.isInteger(cost) || cost <= 0 || !selected) return;

    setSavingPrizeId(prize.id);
    try {
      await editPrize(prize.id, name, cost, selected);
      cancelEditing();
      await refreshTargetState();
    } catch (e) {
      setError(`Failed to update prize: ${e.message}`);
    } finally {
      setSavingPrizeId(null);
    }
  };

  const prizes = targetState?.prizes ?? [];
  const prizeLog = targetState?.prize_log ?? [];
  const balance = targetState?.balance ?? { credits_millis: 0, tokens: 0 };

  return (
    <div>
      <SectionTitle>Admin: manage prize catalogs</SectionTitle>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <div className="panel" style={{ padding: 16, marginBottom: 24, display: "flex", gap: 12, alignItems: "center" }}>
        <span style={{ color: COLORS.creamDim, fontSize: 12, letterSpacing: "0.1em", textTransform: "uppercase" }}>
          Managing prizes for
        </span>
        <select
          value={selected ?? ""}
          disabled={savingPrizeId !== null}
          onChange={(e) => {
            cancelEditing();
            setTargetState(null);
            setSelected(e.target.value || null);
          }}
        >
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

      {selected && (
        <>
          <SectionTitle>Redemption price history</SectionTitle>
          <div className="panel" style={{ padding: 0, marginBottom: 32 }}>
            {!targetState ? (
              <div style={{ padding: 12, color: COLORS.creamDim, fontSize: 13 }}>Loading history…</div>
            ) : prizeLog.length === 0 ? (
              <div style={{ padding: 12, color: COLORS.creamDim, fontSize: 13 }}>
                No redeemed prizes in this user's history.
              </div>
            ) : (
              prizeLog.map((p, i) => (
                <div
                  key={p.id}
                  style={{
                    padding: "12px 18px",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    borderBottom: i < prizeLog.length - 1 ? "1px solid rgba(212,165,72,0.12)" : "none",
                  }}
                >
                  <div>
                    <div style={{ fontSize: 15, color: COLORS.cream }}>{p.name}</div>
                    <div style={{ fontSize: 12, color: COLORS.creamDim, marginTop: 2 }}>
                      {new Date(p.at_ms).toLocaleString([], {
                        month: "short",
                        day: "numeric",
                        year: "numeric",
                        hour: "numeric",
                        minute: "2-digit",
                      })}
                    </div>
                  </div>
                  <div className="mono" style={{ fontSize: 14, color: COLORS.gold }}>
                    {p.cost.toLocaleString()} tokens
                  </div>
                </div>
              ))
            )}
          </div>
        </>
      )}

      <SectionTitle>Existing prizes</SectionTitle>
      <div style={{ color: COLORS.creamDim, fontSize: 12, marginBottom: 16 }}>
        Name and price edits apply to future redemptions; past history keeps its recorded values.
      </div>
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
              disabled={offline || savingPrizeId !== null}
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
            {editingPrizeId === p.id ? (
              <>
                <input
                  aria-label={`Name for ${p.name}`}
                  value={editingName}
                  onChange={(e) => setEditingName(e.target.value)}
                  maxLength={120}
                  disabled={savingPrizeId === p.id}
                  style={{ width: "100%", marginBottom: 8 }}
                />
                <input
                  aria-label={`Token cost for ${p.name}`}
                  type="number"
                  min="1"
                  step="1"
                  value={editingCost}
                  onChange={(e) => setEditingCost(e.target.value)}
                  disabled={savingPrizeId === p.id}
                  style={{ width: "100%", marginBottom: 8 }}
                />
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    className="btn btn-primary"
                    onClick={() => handleEdit(p)}
                    disabled={
                      offline ||
                      savingPrizeId === p.id ||
                      !editingName.trim() ||
                      !Number.isInteger(Number(editingCost)) ||
                      Number(editingCost) <= 0 ||
                      (editingName.trim() === p.name && Number(editingCost) === p.cost)
                    }
                    style={{ padding: "6px 12px", fontSize: 11 }}
                  >
                    {savingPrizeId === p.id ? "Saving…" : "Save"}
                  </button>
                  <button
                    className="btn"
                    onClick={cancelEditing}
                    disabled={savingPrizeId === p.id}
                    style={{ padding: "6px 12px", fontSize: 11 }}
                  >
                    Cancel
                  </button>
                </div>
              </>
            ) : (
              <>
                <div
                  className="display-font"
                  style={{ fontSize: 17, color: COLORS.cream, marginBottom: 8, minHeight: 44 }}
                >
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
                <button
                  className="btn"
                  onClick={() => startEditing(p)}
                  disabled={offline || savingPrizeId !== null}
                  aria-label={`Edit ${p.name}`}
                  style={{ padding: "6px 12px", fontSize: 11, marginTop: 12 }}
                >
                  Edit
                </button>
              </>
            )}
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
