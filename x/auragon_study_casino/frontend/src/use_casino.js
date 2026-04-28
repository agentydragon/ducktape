// Single React hook that exposes the casino's reactive state + mutation
// functions, swallowing the Y.Map/Y.Array iteration boilerplate. Lets
// study_casino.jsx swap its giant useState block for one line:
//
//     const casino = useCasino();
//     casino.credits / casino.tokens / casino.sessions / ...
//     casino.startSession("Biochem"); casino.redeem(prize); ...
//
// All mutations are server-validated through `/ws` (driven by sync.js);
// optimistic updates happen because the local Y.Doc updates synchronously
// while the network round-trip happens in the background.
//
// Active sessions live in the `sessions` Y.Map as entries with no
// `ended_at_ms` field.  `activeSession` is derived by finding the single
// sessions entry whose `ended_at_ms` is absent; `sessions` returns only
// completed entries.

import { casinoSync, Y } from "./sync.js";
import { useYArray, useYMap } from "./y_hooks.js";

export function useCasino() {
  const balance = useYMap(casinoSync.balance);
  const sessionsMap = useYMap(casinoSync.sessions);
  const prizesMap = useYMap(casinoSync.prizes);
  const prizeLogArr = useYArray(casinoSync.prizeLog);

  // Derived plain-JS views that downstream JSX expects unchanged. We round
  // the balance numbers because Yjs stores them as float64.
  //
  // These derivations are deliberately *not* memoized: a deep edit (e.g.,
  // editing a session's subject) does not change `sessionsMap`'s identity
  // or `.size`, and `useMemo([sessionsMap, sessionsMap.size, ...])` would
  // hand back a stale snapshot even though `observeDeep` correctly
  // re-rendered the component. The map iteration is cheap enough to do
  // every render.
  const credits = Math.floor(balance.get("credits") ?? 0);
  const tokens = Math.floor(balance.get("tokens") ?? 0);

  // Completed sessions only — those with ended_at_ms set.
  const sessions = [...sessionsMap.entries()]
    .filter(([, m]) => !!m.get("ended_at_ms"))
    .map(([id, m]) => ({
      id,
      subject: m.get("subject"),
      seconds: Math.floor(m.get("seconds") ?? 0),
      endedAt: Math.floor(m.get("ended_at_ms") ?? 0),
    }))
    .sort((a, b) => b.endedAt - a.endedAt);

  const prizes = [...prizesMap.entries()].map(([id, m]) => ({
    id,
    name: m.get("name"),
    cost: Math.floor(m.get("cost") ?? 0),
  }));

  const prizeLog = prizeLogArr
    .toArray()
    .map((m) => ({
      id: m.get("id"),
      name: m.get("name"),
      cost: Math.floor(m.get("cost") ?? 0),
      at: Math.floor(m.get("at_ms") ?? 0),
    }))
    .sort((a, b) => b.at - a.at);

  // In-progress session: the single sessions entry without ended_at_ms.
  const activeRaw = [...sessionsMap.entries()].find(([, m]) => !m.get("ended_at_ms"));
  const activeSession = activeRaw
    ? {
        id: activeRaw[0],
        subject: activeRaw[1].get("subject"),
        startTime: Math.floor(activeRaw[1].get("start_time_ms") ?? 0),
        paused: !!activeRaw[1].get("paused"),
        pausedDuration: Math.floor(activeRaw[1].get("paused_duration_ms") ?? 0),
        pauseStartedAt: activeRaw[1].get("pause_started_at_ms"),
      }
    : null;

  // === Mutations ===
  // Each one wraps `casinoSync.mutate` (which calls doc.transact) so the
  // UndoManager treats the change as a single unit — important so that a
  // server rejection rolls back the whole user-visible action.

  const startSession = (subject) => {
    if (activeSession) return; // one session at a time — illegal to start while one is running
    const id = `active-${Date.now()}`;
    casinoSync.mutate(() => {
      const sm = new Y.Map();
      casinoSync.sessions.set(id, sm);
      sm.set("subject", subject);
      sm.set("start_time_ms", Date.now());
      sm.set("paused", false);
      sm.set("paused_duration_ms", 0);
      sm.set("pause_started_at_ms", null);
    });
  };

  const pauseSession = () => {
    if (!activeSession || activeSession.paused) return;
    casinoSync.mutate(() => {
      const m = casinoSync.sessions.get(activeSession.id);
      if (!m) return;
      m.set("paused", true);
      m.set("pause_started_at_ms", Date.now());
    });
  };

  const resumeSession = () => {
    if (!activeSession || !activeSession.paused) return;
    const now = Date.now();
    const pausedFor = now - (activeSession.pauseStartedAt ?? now);
    casinoSync.mutate(() => {
      const m = casinoSync.sessions.get(activeSession.id);
      if (!m) return;
      m.set("paused", false);
      m.set("pause_started_at_ms", null);
      m.set("paused_duration_ms", activeSession.pausedDuration + Math.max(0, pausedFor));
    });
  };

  // Helpers used by every mutation that bumps a balance: read the current
  // value from the Y.Map *inside the transaction* so a remote update that
  // landed between render and the actual mutation can't be clobbered.
  const currentCredits = () => Math.floor(casinoSync.balance.get("credits") ?? 0);
  const currentTokens = () => Math.floor(casinoSync.balance.get("tokens") ?? 0);

  const stopSession = () => {
    if (!activeSession) return;
    const sec = elapsedSeconds(activeSession);
    const min = Math.floor(sec / 60);
    casinoSync.mutate(() => {
      if (sec <= 0) {
        // Zero-elapsed sessions are discarded rather than creating noise in history.
        casinoSync.sessions.delete(activeSession.id);
        return;
      }
      const m = casinoSync.sessions.get(activeSession.id);
      if (!m) return;
      m.set("seconds", sec);
      m.set("ended_at_ms", Date.now());
      // Clear in-progress fields to keep the completed entry compact.
      m.delete("start_time_ms");
      m.delete("paused");
      m.delete("paused_duration_ms");
      m.delete("pause_started_at_ms");
      if (min > 0) {
        casinoSync.balance.set("credits", currentCredits() + min);
      }
    });
  };

  const cancelSession = () => {
    if (!activeSession) return;
    casinoSync.mutate(() => casinoSync.sessions.delete(activeSession.id));
  };

  const editSession = (id, updates) => {
    const old = sessions.find((s) => s.id === id);
    if (!old) return;
    const newSec = typeof updates.seconds === "number" ? Math.max(0, updates.seconds) : old.seconds;
    const newSubject = updates.subject || old.subject;
    const delta = Math.floor(newSec / 60) - Math.floor(old.seconds / 60);
    casinoSync.mutate(() => {
      const m = casinoSync.sessions.get(id);
      if (!m) return;
      m.set("subject", newSubject);
      m.set("seconds", newSec);
      if (delta !== 0) {
        casinoSync.balance.set("credits", Math.max(0, currentCredits() + delta));
      }
    });
  };

  const deleteSession = (id) => {
    const old = sessions.find((s) => s.id === id);
    if (!old) return;
    const min = Math.floor(old.seconds / 60);
    casinoSync.mutate(() => {
      casinoSync.sessions.delete(id);
      casinoSync.balance.set("credits", Math.max(0, currentCredits() - min));
    });
  };

  const addPastSession = (subject, seconds, endedAtMs) => {
    if (!subject || seconds <= 0) return;
    const id = `manual-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const min = Math.floor(seconds / 60);
    casinoSync.mutate(() => {
      const sm = new Y.Map();
      casinoSync.sessions.set(id, sm);
      sm.set("subject", subject);
      sm.set("seconds", seconds);
      sm.set("ended_at_ms", endedAtMs);
      casinoSync.balance.set("credits", currentCredits() + min);
    });
  };

  const redeemPrize = (prize) => {
    if (tokens < prize.cost) return;
    const id = `r-${Date.now()}`;
    casinoSync.mutate(() => {
      casinoSync.balance.set("tokens", currentTokens() - prize.cost);
      const entry = new Y.Map();
      casinoSync.prizeLog.push([entry]);
      entry.set("id", id);
      entry.set("name", prize.name);
      entry.set("cost", prize.cost);
      entry.set("at_ms", Date.now());
    });
  };

  const addPrize = (name, cost) => {
    if (!name || cost <= 0) return;
    const id = `p${Date.now()}`;
    casinoSync.mutate(() => {
      const m = new Y.Map();
      casinoSync.prizes.set(id, m);
      m.set("name", name);
      m.set("cost", cost);
    });
  };

  const deletePrize = (id) => {
    casinoSync.mutate(() => casinoSync.prizes.delete(id));
  };

  const convertToTokens = (amount) => {
    const n = Math.max(0, Math.floor(amount));
    if (n <= 0 || n > credits) return;
    casinoSync.mutate(() => {
      casinoSync.balance.set("credits", currentCredits() - n);
      casinoSync.balance.set("tokens", currentTokens() + n);
    });
  };

  // Direct credits / tokens delta helpers (used by the gambling components).
  // These read the current value from Y.Map *inside* the transaction to avoid
  // closure staleness if multiple deltas land within one user action or while
  // a remote update is being applied.
  const addTokens = (delta) => {
    if (delta === 0) return;
    casinoSync.mutate(() => {
      const current = Math.floor(casinoSync.balance.get("tokens") ?? 0);
      casinoSync.balance.set("tokens", Math.max(0, current + delta));
    });
  };
  const addCredits = (delta) => {
    if (delta === 0) return;
    casinoSync.mutate(() => {
      const current = Math.floor(casinoSync.balance.get("credits") ?? 0);
      casinoSync.balance.set("credits", Math.max(0, current + delta));
    });
  };

  const exportData = () => {
    const data = {
      version: 3,
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

  const importData = (data) => {
    casinoSync.mutate(() => {
      casinoSync.balance.set("credits", typeof data.credits === "number" ? data.credits : 0);
      casinoSync.balance.set("tokens", typeof data.tokens === "number" ? data.tokens : 0);
      casinoSync.sessions.clear();
      for (const s of data.sessions ?? []) {
        const sm = new Y.Map();
        casinoSync.sessions.set(s.id, sm);
        sm.set("subject", s.subject);
        sm.set("seconds", s.seconds);
        sm.set("ended_at_ms", s.endedAt);
      }
      if (Array.isArray(data.prizes) && data.prizes.length > 0) {
        casinoSync.prizes.clear();
        for (const p of data.prizes) {
          const pm = new Y.Map();
          casinoSync.prizes.set(p.id, pm);
          pm.set("name", p.name);
          pm.set("cost", p.cost);
        }
      }
      if (Array.isArray(data.prizeLog)) {
        // Y.Array doesn't have a clear() helper that survives every Yjs
        // version; delete in a single pass instead.
        casinoSync.prizeLog.delete(0, casinoSync.prizeLog.length);
        for (const e of data.prizeLog) {
          const pm = new Y.Map();
          casinoSync.prizeLog.push([pm]);
          pm.set("id", e.id);
          pm.set("name", e.name);
          pm.set("cost", e.cost);
          pm.set("at_ms", e.at);
        }
      }
      // Restore active session if present (from v3 export or legacy export).
      if (data.activeSession) {
        const id = `active-${Date.now()}`;
        const sm = new Y.Map();
        casinoSync.sessions.set(id, sm);
        sm.set("subject", data.activeSession.subject);
        sm.set("start_time_ms", data.activeSession.startTime ?? Date.now());
        sm.set("paused", !!data.activeSession.paused);
        sm.set("paused_duration_ms", data.activeSession.pausedDuration ?? 0);
        sm.set("pause_started_at_ms", data.activeSession.pauseStartedAt ?? null);
      }
    });
  };

  const resetData = () => {
    casinoSync.mutate(() => {
      casinoSync.balance.set("credits", 0);
      casinoSync.balance.set("tokens", 0);
      casinoSync.sessions.clear();
      casinoSync.prizeLog.delete(0, casinoSync.prizeLog.length);
      // Prizes intentionally retained; the user re-curates the catalog.
    });
  };

  return {
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
    addTokens,
    addCredits,
    exportData,
    importData,
    resetData,
  };
}

function elapsedSeconds(session) {
  if (!session) return 0;
  const now = Date.now();
  let ms = now - session.startTime - (session.pausedDuration ?? 0);
  if (session.paused && session.pauseStartedAt) ms -= now - session.pauseStartedAt;
  return Math.max(0, ms / 1000);
}
