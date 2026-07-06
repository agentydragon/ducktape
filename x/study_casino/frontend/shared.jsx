import React, { useEffect, useState } from "react";

export const SUBJECTS = [
  "Biochemistry",
  "Anatomy",
  "Physiology",
  "Immunology",
  "Microbiology",
  "Pathophysiology",
  "Pharmacology",
  "Biostatistics & Epi",
  "OMM",
  "Anki",
];

// Red-and-gold casino palette. The felt is a deep crimson; gold and cream are
// the legible foreground accents. `wine` is a slightly brighter accent that
// lifts off the felt; `red` is reserved for danger affordances and roulette
// pockets so it has to remain visibly distinct from the felt's crimson.
export const COLORS = {
  felt: "#5a1f2a",
  feltDark: "#3d1520",
  feltDeep: "#1f0a10",
  gold: "#d4a548",
  goldBright: "#e8b84a",
  goldDim: "#9a7a34",
  cream: "#f5e8c7",
  creamDim: "#c9bc9a",
  wine: "#7a2838",
  red: "#d44040",
  rose: "#e8b4c0",
  black: "#1a1a1a",
};

export function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function fmtHoursMin(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h === 0) return `${m}m`;
  return `${h}h ${m}m`;
}

export function getElapsedSec(session, now = Date.now()) {
  if (!session) return 0;
  let ms = now - session.startTime - (session.pausedDuration || 0);
  if (session.paused && session.pauseStartedAt) ms -= now - session.pauseStartedAt;
  return Math.max(0, ms / 1000);
}

// Server returns server-shaped session fields (id, subject, seconds,
// ended_at_ms); the JSX layer uses camelCase (endedAt). Shared by
// use_casino.js (own sessions) and StatsView's admin cross-user picker.
export function mapSessionRead(row) {
  return {
    id: row.id,
    subject: row.subject,
    seconds: Math.floor(row.seconds ?? 0),
    endedAt: Math.floor(row.ended_at_ms ?? 0),
  };
}

export function bucketBySubject(sessions) {
  const bySubject = {};
  SUBJECTS.forEach((s) => (bySubject[s] = 0));
  sessions.forEach((s) => {
    bySubject[s.subject] = (bySubject[s.subject] || 0) + s.seconds;
  });
  return bySubject;
}

const DAY_MS = 24 * 60 * 60 * 1000;
const WEEK_MS = 7 * DAY_MS;
const DAILY_BUCKET_COUNT = 30;
const WEEKLY_BUCKET_COUNT = 12;

function startOfLocalDay(ms) {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function startOfLocalWeek(ms) {
  const d = new Date(startOfLocalDay(ms));
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); // back to Monday
  return d.getTime();
}

const BUCKET_DATE_FMT = new Intl.DateTimeFormat([], { month: "short", day: "numeric" });

/** Aggregate sessions into fixed-width trailing time buckets ("day" or
 *  "week", in the browser's local timezone) for the study-time graph. */
export function bucketStudyTime(sessions, granularity) {
  const daily = granularity === "day";
  const count = daily ? DAILY_BUCKET_COUNT : WEEKLY_BUCKET_COUNT;
  const step = daily ? DAY_MS : WEEK_MS;
  const latestStart = daily ? startOfLocalDay(Date.now()) : startOfLocalWeek(Date.now());

  const buckets = Array.from({ length: count }, (_, i) => ({
    start: latestStart - (count - 1 - i) * step,
    seconds: 0,
  }));
  const indexByStart = new Map(buckets.map((b, i) => [b.start, i]));

  sessions.forEach((s) => {
    const start = daily ? startOfLocalDay(s.endedAt) : startOfLocalWeek(s.endedAt);
    const idx = indexByStart.get(start);
    if (idx !== undefined) buckets[idx].seconds += s.seconds;
  });

  return buckets.map((b) => ({
    key: b.start,
    label: daily
      ? BUCKET_DATE_FMT.format(b.start)
      : `${BUCKET_DATE_FMT.format(b.start)}–${BUCKET_DATE_FMT.format(b.start + 6 * DAY_MS)}`,
    seconds: b.seconds,
  }));
}

export function SectionTitle({ children, small }) {
  return (
    <div
      className="display-font"
      style={{
        fontSize: small ? 14 : 16,
        color: COLORS.gold,
        letterSpacing: "0.3em",
        textTransform: "uppercase",
        marginBottom: 14,
        fontWeight: 600,
        paddingBottom: 6,
        borderBottom: `1px solid rgba(212,165,72,0.2)`,
      }}
    >
      {children}
    </div>
  );
}

export function StatCard({ label, value, accent }) {
  return (
    <div
      className="deco-corners"
      style={{
        padding: "16px 20px",
        background: "rgba(0,0,0,0.3)",
        border: `1px solid ${accent ? COLORS.gold : "rgba(212,165,72,0.25)"}`,
      }}
    >
      <div
        style={{
          fontSize: 11,
          color: COLORS.creamDim,
          letterSpacing: "0.2em",
          textTransform: "uppercase",
          marginBottom: 6,
        }}
      >
        {label}
      </div>
      <div
        className="display-font mono"
        style={{
          fontSize: 24,
          fontWeight: 700,
          color: accent ? COLORS.gold : COLORS.cream,
        }}
      >
        {value}
      </div>
    </div>
  );
}

const WIN_PARTICLE_COUNT = 28;
const WIN_GLYPHS = ["◆", "★", "♦", "♠", "$", "✦", "♥"];

export function WinBurst({ amount }) {
  const [done, setDone] = useState(false);

  useEffect(() => {
    const id = setTimeout(() => setDone(true), 2400);
    return () => clearTimeout(id);
  }, []);

  if (done) return null;

  const particles = Array.from({ length: WIN_PARTICLE_COUNT }, (_, i) => {
    const angle = (Math.PI * 2 * i) / WIN_PARTICLE_COUNT + (Math.random() - 0.5) * 0.4;
    const dist = 140 + Math.random() * 200;
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist - 90;
    const rot = (Math.random() - 0.5) * 720;
    const delay = Math.random() * 0.18;
    const dur = 1.6 + Math.random() * 0.5;
    const size = 18 + Math.floor(Math.random() * 18);
    const glyph = WIN_GLYPHS[Math.floor(Math.random() * WIN_GLYPHS.length)];
    const goldenTone = Math.random() > 0.35;
    return { dx, dy, rot, delay, dur, size, glyph, goldenTone };
  });

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        pointerEvents: "none",
        overflow: "hidden",
        zIndex: 100,
      }}
    >
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: `radial-gradient(circle at center, ${COLORS.goldBright}, transparent 70%)`,
          animation: "win-flash 0.9s ease-out forwards",
        }}
      />
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          width: 120,
          height: 120,
          marginLeft: -60,
          marginTop: -60,
          borderRadius: "50%",
          border: `3px solid ${COLORS.goldBright}`,
          boxShadow: `0 0 40px ${COLORS.goldBright}`,
          animation: "win-ring 1s ease-out forwards",
        }}
      />
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          width: 0,
          height: 0,
        }}
      >
        {particles.map((p, i) => (
          <span
            key={i}
            style={{
              position: "absolute",
              left: 0,
              top: 0,
              fontSize: p.size,
              fontFamily: "'Playfair Display', Georgia, serif",
              fontWeight: 700,
              color: p.goldenTone ? COLORS.goldBright : COLORS.rose,
              textShadow: `0 0 8px ${p.goldenTone ? COLORS.gold : COLORS.rose}`,
              animation: `win-particle ${p.dur}s cubic-bezier(0.2, 0.6, 0.4, 1) ${p.delay}s forwards`,
              willChange: "transform, opacity",
              "--dx": `${p.dx}px`,
              "--dy": `${p.dy}px`,
              "--rot": `${p.rot}deg`,
            }}
          >
            {p.glyph}
          </span>
        ))}
      </div>
      <div
        className="display-font"
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          fontSize: 56,
          fontWeight: 900,
          color: COLORS.goldBright,
          textShadow: `0 0 30px ${COLORS.goldBright}, 0 0 12px ${COLORS.goldBright}, 0 4px 8px rgba(0,0,0,0.6)`,
          letterSpacing: "0.05em",
          whiteSpace: "nowrap",
          animation: "win-text-pop 1.8s cubic-bezier(0.2, 0.7, 0.3, 1) forwards",
        }}
      >
        +{amount.toLocaleString()} <span style={{ fontSize: 24, color: COLORS.rose }}>tokens</span>
      </div>
    </div>
  );
}

// Returns a "YYYY-MM-DDTHH:MM" string in the user's local timezone, suitable
// for the value of an <input type="datetime-local">. The native Date.toISO
// methods return UTC, which would shift the picker by the browser's offset.
export function localDatetimeInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

export function AddPastSessionForm({ offline, onAdd }) {
  const [subject, setSubject] = useState(SUBJECTS[0]);
  const [hours, setHours] = useState(0);
  const [minutes, setMinutes] = useState(30);
  const [endedAt, setEndedAt] = useState(() => localDatetimeInputValue(new Date()));

  const seconds = Math.max(0, parseInt(hours) || 0) * 3600 + Math.max(0, Math.min(59, parseInt(minutes) || 0)) * 60;
  // Browsers parse "YYYY-MM-DDTHH:MM" as local time, which is what the picker
  // shows the user — no manual TZ adjustment needed.
  const endedAtMs = new Date(endedAt).getTime();
  const canAdd = !offline && subject && seconds > 0 && Number.isFinite(endedAtMs);

  const handleAdd = () => {
    if (!canAdd) return;
    onAdd(subject, seconds, endedAtMs);
    setHours(0);
    setMinutes(30);
    setEndedAt(localDatetimeInputValue(new Date()));
  };

  const minutesEarned = Math.floor(seconds / 60);

  return (
    <div className="panel" style={{ padding: 18, marginBottom: 32 }}>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(140px, 1.4fr) minmax(180px, 1fr) minmax(200px, 1.2fr) auto",
          gap: 10,
          alignItems: "end",
        }}
      >
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
            Subject
          </div>
          <select value={subject} onChange={(e) => setSubject(e.target.value)} style={{ width: "100%" }}>
            {SUBJECTS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
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
            Duration
          </div>
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            <input
              type="number"
              value={hours}
              onChange={(e) => setHours(e.target.value)}
              min="0"
              style={{ width: 64 }}
            />
            <span style={{ fontSize: 12, color: COLORS.creamDim }}>h</span>
            <input
              type="number"
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              min="0"
              max="59"
              style={{ width: 64 }}
            />
            <span style={{ fontSize: 12, color: COLORS.creamDim }}>m</span>
          </div>
        </div>
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
            Ended at
          </div>
          <input
            type="datetime-local"
            value={endedAt}
            onChange={(e) => setEndedAt(e.target.value)}
            style={{ width: "100%" }}
          />
        </div>
        <button className="btn btn-primary" onClick={handleAdd} disabled={!canAdd}>
          Add session
        </button>
      </div>
      <div style={{ fontSize: 11, color: COLORS.creamDim, marginTop: 10 }}>
        {canAdd ? (
          <>
            Will log {fmtHoursMin(seconds)} of {subject} ending{" "}
            {new Date(endedAtMs).toLocaleString([], {
              month: "short",
              day: "numeric",
              hour: "numeric",
              minute: "2-digit",
            })}
            . Earns <strong style={{ color: COLORS.gold }}>+{minutesEarned} credits</strong>.
          </>
        ) : (
          "Pick a subject, duration, and end time."
        )}
      </div>
    </div>
  );
}

export function SessionRow({ session, isLast, offline, onEdit, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [subject, setSubject] = useState(session.subject);
  const [hours, setHours] = useState(Math.floor(session.seconds / 3600));
  const [minutes, setMinutes] = useState(Math.floor((session.seconds % 3600) / 60));

  const startEdit = () => {
    setSubject(session.subject);
    setHours(Math.floor(session.seconds / 3600));
    setMinutes(Math.floor((session.seconds % 3600) / 60));
    setEditing(true);
    setConfirmDel(false);
  };

  const saveEdit = () => {
    const h = Math.max(0, parseInt(hours) || 0);
    const m = Math.max(0, Math.min(59, parseInt(minutes) || 0));
    const newSeconds = h * 3600 + m * 60;
    onEdit(session.id, { subject, seconds: newSeconds });
    setEditing(false);
  };

  const handleDelete = () => {
    if (confirmDel) {
      onDelete(session.id);
    } else {
      setConfirmDel(true);
      setTimeout(() => setConfirmDel(false), 3000);
    }
  };

  const baseStyle = {
    padding: "10px 18px",
    borderBottom: isLast ? "none" : `1px solid rgba(212,165,72,0.08)`,
    fontSize: 13,
  };

  if (editing) {
    return (
      <div style={{ ...baseStyle, background: "rgba(212,165,72,0.05)" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            style={{ flex: "1 1 140px", minWidth: 120 }}
          >
            {SUBJECTS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            <input
              type="number"
              value={hours}
              onChange={(e) => setHours(e.target.value)}
              min="0"
              style={{ width: 56 }}
            />
            <span style={{ fontSize: 12, color: COLORS.creamDim }}>h</span>
            <input
              type="number"
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              min="0"
              max="59"
              style={{ width: 56 }}
            />
            <span style={{ fontSize: 12, color: COLORS.creamDim }}>m</span>
          </div>
          <div style={{ display: "flex", gap: 4 }}>
            <button
              className="btn btn-primary"
              style={{ padding: "6px 12px", fontSize: 11 }}
              onClick={saveEdit}
              disabled={offline}
            >
              Save
            </button>
            <button className="btn" style={{ padding: "6px 12px", fontSize: 11 }} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
        <div style={{ fontSize: 11, color: COLORS.creamDim, marginTop: 6 }}>
          Current: {fmtHoursMin(session.seconds)} ({Math.floor(session.seconds / 60)} cr). Changing duration will adjust
          your credit balance by the difference.
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        ...baseStyle,
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div style={{ minWidth: 0, flex: 1 }}>
        <span style={{ color: COLORS.cream }}>{session.subject}</span>
        <span style={{ color: COLORS.creamDim, marginLeft: 12, fontSize: 12 }}>
          {new Date(session.endedAt).toLocaleString([], {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}
        </span>
      </div>
      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <span className="mono" style={{ color: COLORS.cream }}>
          {fmtHoursMin(session.seconds)}
        </span>
        <span className="mono" style={{ color: COLORS.gold, minWidth: 56, textAlign: "right", fontSize: 12 }}>
          +{Math.floor(session.seconds / 60)} cr
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            onClick={startEdit}
            disabled={offline}
            title="Edit session"
            style={{
              width: 26,
              height: 26,
              padding: 0,
              background: "transparent",
              border: `1px solid rgba(212,165,72,0.3)`,
              color: offline ? "rgba(201,188,154,0.3)" : COLORS.creamDim,
              cursor: offline ? "not-allowed" : "pointer",
              opacity: offline ? 0.35 : 1,
              fontSize: 12,
              borderRadius: 2,
            }}
          >
            ✎
          </button>
          <button
            onClick={handleDelete}
            disabled={offline}
            title={confirmDel ? "Click again to confirm" : "Delete session"}
            style={{
              width: confirmDel ? "auto" : 26,
              height: 26,
              padding: confirmDel ? "0 8px" : 0,
              background: confirmDel ? COLORS.red : "transparent",
              border: `1px solid ${confirmDel ? COLORS.red : "rgba(178,57,57,0.4)"}`,
              color: offline ? "rgba(212,64,64,0.3)" : confirmDel ? COLORS.cream : COLORS.red,
              cursor: offline ? "not-allowed" : "pointer",
              opacity: offline ? 0.35 : 1,
              fontSize: confirmDel ? 10 : 14,
              borderRadius: 2,
              letterSpacing: confirmDel ? "0.1em" : 0,
              textTransform: confirmDel ? "uppercase" : "none",
              fontWeight: confirmDel ? 600 : 400,
            }}
          >
            {confirmDel ? "Confirm" : "×"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function SubjectBreakdownBars({ sessions }) {
  const bySubject = bucketBySubject(sessions);
  const maxVal = Math.max(1, ...Object.values(bySubject));
  return (
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
  );
}

const GRAPH_GRANULARITIES = [
  { key: "day", label: "Daily" },
  { key: "week", label: "Weekly" },
];

export function StudyTimeGraph({ sessions }) {
  const [granularity, setGranularity] = useState("day");
  const [hoverKey, setHoverKey] = useState(null);
  const buckets = bucketStudyTime(sessions, granularity);
  const maxSeconds = Math.max(1, ...buckets.map((b) => b.seconds));
  const hovered = buckets.find((b) => b.key === hoverKey) ?? null;

  return (
    <div className="panel" style={{ padding: 20, marginBottom: 40 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 18,
          flexWrap: "wrap",
          gap: 10,
        }}
      >
        <div style={{ fontSize: 12, color: COLORS.creamDim }}>
          {hovered ? (
            <>
              <span style={{ color: COLORS.gold }}>{hovered.label}</span>: {fmtHoursMin(hovered.seconds)}
            </>
          ) : (
            `Last ${buckets.length} ${granularity === "day" ? "days" : "weeks"}`
          )}
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {GRAPH_GRANULARITIES.map((g) => (
            <button
              key={g.key}
              onClick={() => setGranularity(g.key)}
              className="btn"
              style={{
                padding: "4px 14px",
                fontSize: 11,
                background: granularity === g.key ? COLORS.gold : "transparent",
                color: granularity === g.key ? COLORS.feltDeep : COLORS.gold,
              }}
            >
              {g.label}
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "flex-end", gap: granularity === "day" ? 3 : 10, height: 130 }}>
        {buckets.map((b) => {
          const pct = b.seconds > 0 ? Math.max((b.seconds / maxSeconds) * 100, 3) : 0;
          return (
            <div
              key={b.key}
              onMouseEnter={() => setHoverKey(b.key)}
              onMouseLeave={() => setHoverKey((k) => (k === b.key ? null : k))}
              title={`${b.label}: ${fmtHoursMin(b.seconds)}`}
              style={{ flex: 1, minWidth: 2, height: "100%", display: "flex", alignItems: "flex-end" }}
            >
              <div
                style={{
                  width: "100%",
                  height: `${pct}%`,
                  borderRadius: "2px 2px 0 0",
                  background:
                    b.key === hoverKey
                      ? `linear-gradient(180deg, ${COLORS.cream}, ${COLORS.goldBright})`
                      : b.seconds > 0
                        ? `linear-gradient(180deg, ${COLORS.goldBright}, ${COLORS.goldDim})`
                        : "rgba(255,255,255,0.06)",
                  transition: "height 0.3s",
                }}
              />
            </div>
          );
        })}
      </div>
      <div
        style={{ display: "flex", justifyContent: "space-between", marginTop: 8, fontSize: 10, color: COLORS.creamDim }}
      >
        <span>{buckets[0]?.label}</span>
        <span>{buckets[buckets.length - 1]?.label}</span>
      </div>
    </div>
  );
}

/** "By subject" totals + daily/weekly study-time graph for one session list.
 *  Shared by StatsView's own data and its admin cross-user picker. */
export function StudyHabitsSections({ sessions }) {
  if (sessions.length === 0) {
    return <div style={{ fontSize: 13, color: COLORS.creamDim, marginBottom: 40 }}>No sessions logged yet.</div>;
  }
  return (
    <>
      <SectionTitle>By Subject</SectionTitle>
      <SubjectBreakdownBars sessions={sessions} />

      <SectionTitle>Over Time</SectionTitle>
      <StudyTimeGraph sessions={sessions} />
    </>
  );
}
