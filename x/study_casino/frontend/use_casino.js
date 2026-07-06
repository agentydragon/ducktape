// Single React hook exposing reactive state + every mutation. The data
// model is server-authoritative Postgres — no CRDT — and the active
// study-session timer lives in localStorage on the client (only
// `/actions/session/complete` ever turns it into a server-side row).
//
//     const casino = useCasino();
//     casino.credits / casino.tokens / casino.sessions / ...
//     casino.startSession("Biochem");        // local
//     casino.stopSession();                  // server action
//     casino.redeemPrize(prize);             // server action
//
// study_casino.jsx consumes the public surface below; keep it stable when
// swapping the underlying transport.

import { useEffect, useState } from "react";

import { casinoSync, useCasinoState, useSyncStatus, useMe, useDeploymentInfo } from "./sync.js";
import { mapSessionRead } from "./shared.jsx";

const ACTIVE_SESSION_LS_KEY = "casino:active_session";

function readActiveSession() {
  try {
    const raw = window.localStorage.getItem(ACTIVE_SESSION_LS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed?.subject !== "string" || typeof parsed?.startTime !== "number") return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeActiveSession(session) {
  if (session === null) {
    window.localStorage.removeItem(ACTIVE_SESSION_LS_KEY);
  } else {
    window.localStorage.setItem(ACTIVE_SESSION_LS_KEY, JSON.stringify(session));
  }
  // Notify all useActiveSession() subscribers in this tab. The
  // `storage` event only fires across tabs, not within the same tab.
  window.dispatchEvent(new CustomEvent("casino:active_session_changed"));
}

function useActiveSession() {
  const [session, setSession] = useState(readActiveSession());
  useEffect(() => {
    const refresh = () => setSession(readActiveSession());
    window.addEventListener("casino:active_session_changed", refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener("casino:active_session_changed", refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);
  return session;
}

const newActionId = (prefix) =>
  typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? `${prefix}:${crypto.randomUUID()}`
    : `${prefix}:${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

const postActionResult = async (path, body) => {
  const response = await casinoSync.postAction(path, body);
  return response.result;
};

export function useCasino() {
  const state = useCasinoState();
  const activeSession = useActiveSession();
  const syncStatus = useSyncStatus();
  const me = useMe();
  const deploymentInfo = useDeploymentInfo();
  const offline = syncStatus.kind === "offline";
  const isAdmin = !!me?.is_admin;
  const username = me?.username ?? null;

  const balance = state?.balance ?? { credits: 0, tokens: 0 };
  const credits = Math.floor(balance.credits ?? 0);
  const tokens = Math.floor(balance.tokens ?? 0);

  // Server returns server-shaped fields (ended_at_ms, at_ms); the JSX layer
  // uses camelCase aliases (endedAt, at) — translate at this seam.
  const sessions = (state?.sessions ?? []).map(mapSessionRead);
  const prizes = (state?.prizes ?? []).map((row) => ({
    id: row.id,
    name: row.name,
    cost: Math.floor(row.cost ?? 0),
  }));
  const prizeLog = (state?.prize_log ?? []).map((row) => ({
    id: row.id,
    name: row.name,
    cost: Math.floor(row.cost ?? 0),
    at: Math.floor(row.at_ms ?? 0),
  }));

  // === Local-only active-session lifecycle ===
  const startSession = (subject) => {
    if (activeSession) return;
    writeActiveSession({
      subject,
      startTime: Date.now(),
      paused: false,
      pausedDuration: 0,
      pauseStartedAt: null,
    });
  };

  const pauseSession = () => {
    if (!activeSession || activeSession.paused) return;
    writeActiveSession({ ...activeSession, paused: true, pauseStartedAt: Date.now() });
  };

  const resumeSession = () => {
    if (!activeSession || !activeSession.paused) return;
    const now = Date.now();
    const pausedFor = now - (activeSession.pauseStartedAt ?? now);
    writeActiveSession({
      ...activeSession,
      paused: false,
      pauseStartedAt: null,
      pausedDuration: (activeSession.pausedDuration ?? 0) + Math.max(0, pausedFor),
    });
  };

  const cancelSession = () => {
    writeActiveSession(null);
  };

  // === Server actions ===
  const stopSession = async () => {
    if (!activeSession) return;
    const endedAt = Date.now();
    try {
      await casinoSync.postAction("/actions/session/complete", {
        client_action_id: newActionId("session.complete"),
        subject: activeSession.subject,
        start_time_ms: activeSession.startTime,
        paused_duration_ms: activeSession.pausedDuration ?? 0,
        ended_at_ms: endedAt,
      });
      writeActiveSession(null);
    } catch {
      // postAction surfaced the error already; keep the active session so the user can retry.
    }
  };

  const editSession = (id, updates) => {
    const old = sessions.find((s) => s.id === id);
    if (!old) return;
    const newSec = typeof updates.seconds === "number" ? Math.max(0, updates.seconds) : old.seconds;
    const newSubject = updates.subject || old.subject;
    return casinoSync.postAction("/actions/session/edit", {
      client_action_id: newActionId("session.edit"),
      session_id: id,
      subject: newSubject,
      seconds: newSec,
    });
  };

  const deleteSession = (id) => {
    const old = sessions.find((s) => s.id === id);
    if (!old) return;
    return casinoSync.postAction("/actions/session/delete", {
      client_action_id: newActionId("session.delete"),
      session_id: id,
    });
  };

  const addPastSession = (subject, seconds, endedAtMs) => {
    if (!subject || seconds <= 0) return;
    return casinoSync.postAction("/actions/session/add-past", {
      client_action_id: newActionId("session.add"),
      subject,
      seconds,
      ended_at_ms: endedAtMs,
    });
  };

  const redeemPrize = (prize) => {
    if (tokens < prize.cost) return;
    return casinoSync.postAction("/actions/prize/redeem", {
      client_action_id: newActionId("prize.redeem"),
      prize_id: prize.id,
    });
  };

  const addPrize = (name, cost, targetUser = null) => {
    if (!name || cost <= 0) return;
    return casinoSync.postAction("/actions/prize/create", {
      client_action_id: newActionId("prize.create"),
      name,
      cost: Math.floor(cost),
      ...(targetUser ? { target_user: targetUser } : {}),
    });
  };

  const deletePrize = (id, targetUser = null) =>
    casinoSync.postAction("/actions/prize/delete", {
      client_action_id: newActionId("prize.delete"),
      prize_id: id,
      ...(targetUser ? { target_user: targetUser } : {}),
    });

  const convertToTokens = (amount) => {
    const n = Math.max(0, Math.floor(amount));
    if (n <= 0 || n > credits) return;
    return casinoSync.postAction("/actions/convert", {
      client_action_id: newActionId("convert"),
      amount: n,
    });
  };

  const spinSlots = (wagerCredits) =>
    postActionResult("/casino/slots/spin", {
      client_action_id: newActionId("slots.spin"),
      wager_credits: Math.floor(wagerCredits),
    });

  const spinRoulette = ({ wagerCredits, betType, betNumber }) =>
    postActionResult("/casino/roulette/spin", {
      client_action_id: newActionId("roulette.spin"),
      wager_credits: Math.floor(wagerCredits),
      bet_type: betType,
      bet_number: betType === "number" ? betNumber : null,
    });

  const blackjackDeal = (wagerCredits) =>
    postActionResult("/casino/blackjack/deal", {
      client_action_id: newActionId("blackjack.deal"),
      wager_credits: Math.floor(wagerCredits),
    });

  const blackjackHit = (handId) =>
    postActionResult("/casino/blackjack/hit", {
      client_action_id: newActionId("blackjack.hit"),
      hand_id: handId,
    });

  const blackjackStand = (handId) =>
    postActionResult("/casino/blackjack/stand", {
      client_action_id: newActionId("blackjack.stand"),
      hand_id: handId,
    });

  const blackjackDouble = (handId) =>
    postActionResult("/casino/blackjack/double", {
      client_action_id: newActionId("blackjack.double"),
      hand_id: handId,
    });

  const exportData = () => {
    const data = {
      version: 4,
      exportedAt: new Date().toISOString(),
      credits,
      tokens,
      sessions,
      prizes,
      prizeLog,
      activeSession,
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `study-casino-backup-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const importData = (data) =>
    casinoSync.postAction("/actions/import", {
      client_action_id: newActionId("data.import"),
      data,
    });

  const resetData = () =>
    casinoSync.postAction("/actions/reset", {
      client_action_id: newActionId("data.reset"),
    });

  return {
    offline,
    deploymentInfo,
    username,
    isAdmin,
    credits,
    tokens,
    sessions,
    prizes,
    prizeLog,
    activeSession,
    startSession,
    pauseSession,
    resumeSession,
    stopSession,
    cancelSession,
    editSession,
    deleteSession,
    addPastSession,
    redeemPrize,
    addPrize,
    deletePrize,
    convertToTokens,
    spinSlots,
    spinRoulette,
    blackjackDeal,
    blackjackHit,
    blackjackStand,
    blackjackDouble,
    exportData,
    importData,
    resetData,
  };
}
