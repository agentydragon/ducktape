// Visible status surface for the multi-device sync. Replaces the old
// "✓ Saved" pill that flashed on every successful append.
//
// Two layers:
//   1. A persistent slim banner pinned under the header that renders
//      anything other than `kind: "ok"`. The banner has a "Retry now"
//      button when state is `offline` or `rejected`.
//   2. A short-lived toast that appears whenever a *new* rejection lands
//      (keyed by `rejection.id`).
//
// All copy is intentionally explicit: stale views, server errors, and
// constraint violations each get a plain-language explanation rather
// than a silent log line.

import React, { useEffect, useState } from "react";

import { casinoSync } from "./sync.js";
import { useSyncRejection, useSyncStatus } from "./y_hooks.js";

const COLORS = {
  gold: "#d4a548",
  goldBright: "#e8b84a",
  cream: "#f5e8c7",
  creamDim: "#c9bc9a",
  red: "#d44040",
  rose: "#e8b4c0",
  feltDeep: "#1f0a10",
  wine: "#7a2838",
};

function bannerStyle(bg, border, color) {
  return {
    padding: "8px 18px",
    fontSize: 12,
    letterSpacing: "0.1em",
    textTransform: "uppercase",
    fontFamily: "'Outfit', sans-serif",
    background: bg,
    borderBottom: `1px solid ${border}`,
    color,
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  };
}

function RetryButton() {
  return (
    <button
      type="button"
      onClick={() => casinoSync.syncOnce()}
      style={{
        padding: "4px 12px",
        background: "transparent",
        color: "inherit",
        border: "1px solid currentColor",
        cursor: "pointer",
        fontSize: 11,
        letterSpacing: "0.15em",
        textTransform: "uppercase",
        fontFamily: "'Outfit', sans-serif",
      }}
    >
      Retry now
    </button>
  );
}

export function SyncBanner() {
  const status = useSyncStatus();
  const rejection = useSyncRejection();
  const [showRejectionToast, setShowRejectionToast] = useState(null);

  // Toast = transient highlight whenever a *new* rejection arrives.
  useEffect(() => {
    if (!rejection) return;
    setShowRejectionToast(rejection);
    const id = setTimeout(() => setShowRejectionToast(null), 6000);
    return () => clearTimeout(id);
  }, [rejection?.id]);

  return (
    <>
      {status.kind === "offline" && (
        <div data-testid="sync-banner-offline" style={bannerStyle("rgba(122,40,56,0.6)", COLORS.red, COLORS.cream)}>
          <span>
            Sync paused — <strong>{status.reason}</strong>. Your changes are saved locally and will replay on reconnect.
          </span>
          <RetryButton />
        </div>
      )}
      {status.kind === "rejected" && (
        <div data-testid="sync-banner-rejected" style={bannerStyle("rgba(212,64,64,0.4)", COLORS.red, COLORS.cream)}>
          <span>
            Server rejected the last action ({status.rule}): {status.message}
          </span>
          <RetryButton />
        </div>
      )}
      {status.kind === "syncing" && (
        <div
          data-testid="sync-banner-syncing"
          style={bannerStyle("rgba(122,40,56,0.25)", COLORS.wine, COLORS.creamDim)}
        >
          <span>Syncing with server…</span>
        </div>
      )}

      {showRejectionToast && (
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
          <div>{showRejectionToast.message}</div>
          <div style={{ color: COLORS.creamDim, fontSize: 11, marginTop: 6 }}>(rule: {showRejectionToast.rule})</div>
        </div>
      )}
    </>
  );
}
