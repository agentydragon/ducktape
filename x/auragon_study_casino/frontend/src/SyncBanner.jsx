import React, { useEffect, useState } from "react";

import { casinoSync } from "./sync.js";
import { useSyncRejection, useSyncStatus } from "./y_hooks.js";

const COLORS = {
  gold: "#d4a548",
  cream: "#f5e8c7",
  creamDim: "#c9bc9a",
  red: "#d44040",
  feltDeep: "#1f0a10",
};

function iconProps(status) {
  switch (status.kind) {
    case "ok": {
      const t = status.lastSyncedAt ? new Date(status.lastSyncedAt).toLocaleTimeString() : "";
      return {
        glyph: "✓",
        color: "rgba(180,220,150,0.35)",
        title: `Synced${t ? ` at ${t}` : ""}`,
        spin: false,
        testid: "sync-icon-ok",
        clickable: false,
      };
    }
    case "syncing":
      return {
        glyph: "↻",
        color: COLORS.creamDim,
        title: "Syncing with server…",
        spin: true,
        testid: "sync-banner-syncing",
        clickable: false,
      };
    case "offline":
      return {
        glyph: "⚡",
        color: COLORS.red,
        title: `Offline — ${status.reason} (click to retry)`,
        spin: false,
        testid: "sync-banner-offline",
        clickable: true,
      };
    case "rejected":
      return {
        glyph: "⚠",
        color: COLORS.red,
        title: `Rejected (${status.rule}): ${status.message} (click to retry)`,
        spin: false,
        testid: "sync-banner-rejected",
        clickable: true,
      };
    default:
      return {
        glyph: "·",
        color: COLORS.creamDim,
        title: "",
        spin: false,
        testid: "sync-icon-unknown",
        clickable: false,
      };
  }
}

const SPIN_KEYFRAMES = `@keyframes sync-spin { to { transform: rotate(360deg); } }`;

export function SyncIcon() {
  const status = useSyncStatus();
  const rejection = useSyncRejection();
  const [toast, setToast] = useState(null);

  useEffect(() => {
    if (!rejection) return;
    setToast(rejection);
    const id = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(id);
  }, [rejection?.id]);

  const { glyph, color, title, spin, testid, clickable } = iconProps(status);

  const iconStyle = {
    width: 22,
    height: 22,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 15,
    lineHeight: 1,
    color,
    animation: spin ? "sync-spin 1.4s linear infinite" : undefined,
    flexShrink: 0,
    background: "none",
    border: "none",
    padding: 0,
    cursor: clickable ? "pointer" : "default",
  };

  return (
    <>
      <style>{SPIN_KEYFRAMES}</style>
      {clickable ? (
        <button
          type="button"
          data-testid={testid}
          title={title}
          aria-label={title}
          onClick={() => casinoSync.syncOnce()}
          style={iconStyle}
        >
          {glyph}
        </button>
      ) : (
        <div data-testid={testid} title={title} aria-label={title} style={iconStyle}>
          {glyph}
        </div>
      )}

      {toast && (
        <div
          data-testid="sync-toast-rejection"
          style={{
            position: "fixed",
            top: 24,
            right: 24,
            zIndex: 200,
            maxWidth: 360,
            padding: "14px 18px",
            background: COLORS.feltDeep,
            border: `1px solid ${COLORS.red}`,
            borderRadius: 4,
            color: COLORS.cream,
            fontFamily: "'Outfit', sans-serif",
            fontSize: 13,
            boxShadow: "0 8px 24px rgba(0,0,0,0.6)",
          }}
        >
          <div style={{ color: COLORS.red, fontWeight: 600, marginBottom: 6 }}>Action rolled back</div>
          <div>{toast.message}</div>
          <div style={{ color: "rgba(180,180,160,0.7)", fontSize: 11, marginTop: 6 }}>rule: {toast.rule}</div>
        </div>
      )}
    </>
  );
}
