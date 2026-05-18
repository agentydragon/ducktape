import React, { useEffect, useState, useCallback } from "react";

import { COLORS, SectionTitle } from "./shared.jsx";
import { fetchCasinoStats, useCasinoState } from "./sync.js";

const PCT_FORMATTER = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

const fmtPct = (v) => (v === null || v === undefined ? "—" : PCT_FORMATTER.format(v));
const fmtRtp = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const fmtEv = (v) => {
  if (v === null || v === undefined) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(3)}`;
};
const fmtInt = (n) => (n ?? 0).toLocaleString();

export function CasinoStatsView({ isAdmin }) {
  const initialTarget = new URLSearchParams(window.location.search).get("user") ?? "";
  const [targetUser, setTargetUser] = useState(initialTarget);
  const [stats, setStats] = useState(null);
  const [error, setError] = useState(null);
  // The /state cache pings when any tab plays a game — use it to retrigger
  // a stats refetch so the page live-updates without manual refresh.
  const stateCache = useCasinoState();

  const refresh = useCallback(async () => {
    try {
      const user = isAdmin && targetUser.trim() ? targetUser.trim() : null;
      const result = await fetchCasinoStats(user);
      setStats(result);
      setError(null);
    } catch (e) {
      setError(e.message ?? String(e));
      setStats(null);
    }
  }, [isAdmin, targetUser]);

  useEffect(() => {
    refresh();
  }, [refresh, stateCache]);

  return (
    <div>
      <SectionTitle>Casino payout history</SectionTitle>

      <div style={{ fontSize: 12, color: COLORS.creamDim, marginBottom: 16, lineHeight: 1.5 }}>
        Empirical stats over every server-resolved spin / hand. Coverage starts{" "}
        <span style={{ color: COLORS.gold }}>{stats?.since_date ?? "2026-05-07"}</span> (the client-reported →
        server-resolved cutover). Wager amounts are in credits, payouts in tokens; RTP is{" "}
        <span className="mono">returned / wagered</span>, EV / credit is{" "}
        <span className="mono">(returned − wagered) / wagered</span>.
      </div>

      {isAdmin && (
        <div
          className="panel"
          style={{ padding: 12, marginBottom: 18, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}
        >
          <span style={{ color: COLORS.creamDim, fontSize: 12, letterSpacing: "0.1em", textTransform: "uppercase" }}>
            Viewing stats for
          </span>
          <input
            value={targetUser}
            onChange={(e) => setTargetUser(e.target.value)}
            placeholder="(yourself)"
            style={{ minWidth: 200 }}
          />
          <button className="btn" onClick={refresh}>
            Reload
          </button>
        </div>
      )}

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

      {stats && (
        <>
          <div style={{ fontSize: 12, color: COLORS.creamDim, marginBottom: 18 }}>
            <span style={{ color: COLORS.gold }}>{fmtInt(stats.event_count)}</span> resolved games for{" "}
            <span style={{ color: COLORS.cream }}>{stats.username}</span>.
          </div>

          {stats.games.map((g) => (
            <GameStatsCard key={g.game} game={g} />
          ))}
        </>
      )}
    </div>
  );
}

function GameStatsCard({ game }) {
  return (
    <div style={{ marginBottom: 32 }}>
      <SectionTitle small>{game.game}</SectionTitle>

      {game.total.count === 0 ? (
        <div style={{ fontSize: 13, color: COLORS.creamDim, marginBottom: 12 }}>No resolved games yet.</div>
      ) : game.blackjack ? (
        <>
          <BlackjackPanel total={game.total} stats={game.blackjack} />
          {game.timeline.length > 0 && <TimelineTable timeline={game.timeline} />}
        </>
      ) : (
        <>
          <BucketTable buckets={[game.total, ...game.buckets]} />
          {game.timeline.length > 0 && <TimelineTable timeline={game.timeline} />}
        </>
      )}
    </div>
  );
}

function BlackjackPanel({ total, stats }) {
  const { summary, outcome_freq, by_dealer_upcard, by_doubled } = stats;
  return (
    <>
      <SummaryPanel total={total} summary={summary} />
      <OutcomeFreqTable rows={outcome_freq} />
      <SliceTable title="By dealer upcard" rows={by_dealer_upcard} keyLabel="Upcard" />
      <SliceTable title="Doubled vs not" rows={by_doubled} keyLabel="" />
    </>
  );
}

function SummaryPanel({ total, summary }) {
  const stat = (label, value, color) => (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", color: COLORS.creamDim }}>
        {label}
      </span>
      <span className="mono" style={{ fontSize: 15, color: color ?? COLORS.cream }}>
        {value}
      </span>
    </div>
  );
  const netColor = total.net > 0 ? COLORS.goldBright : total.net < 0 ? COLORS.red : COLORS.cream;
  const evColor =
    total.ev_per_credit === null || total.ev_per_credit === undefined
      ? COLORS.creamDim
      : total.ev_per_credit > 0
        ? COLORS.goldBright
        : total.ev_per_credit < 0
          ? COLORS.red
          : COLORS.cream;
  return (
    <div className="panel" style={{ padding: 14, marginBottom: 14, display: "flex", flexWrap: "wrap", gap: 22 }}>
      {stat("Plays", fmtInt(summary.count))}
      {stat("W / L / P", `${fmtInt(summary.wins)} / ${fmtInt(summary.losses)} / ${fmtInt(summary.pushes)}`)}
      {stat("Win rate (excl. push)", fmtPct(summary.win_rate_excl_push))}
      {stat("Blackjack rate", fmtPct(summary.blackjack_rate))}
      {stat("Wagered", fmtInt(total.wagered))}
      {stat("Returned", fmtInt(total.returned))}
      {stat("Net", `${total.net > 0 ? "+" : ""}${fmtInt(total.net)}`, netColor)}
      {stat("Emp. RTP", fmtRtp(total.rtp))}
      {stat("EV / credit", fmtEv(total.ev_per_credit), evColor)}
    </div>
  );
}

function OutcomeFreqTable({ rows }) {
  return (
    <div className="panel" style={{ padding: 12, overflowX: "auto", marginBottom: 14 }}>
      <div style={{ fontSize: 11, color: COLORS.creamDim, marginBottom: 8, letterSpacing: "0.1em" }}>
        OUTCOME FREQUENCY
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr style={{ color: COLORS.creamDim, textAlign: "right" }}>
            <th style={{ textAlign: "left", padding: "6px 8px" }}>Outcome</th>
            <th style={{ padding: "6px 8px" }}>Plays</th>
            <th style={{ padding: "6px 8px" }}>Frequency</th>
            <th style={{ padding: "6px 8px" }}>Avg wager</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} style={{ borderTop: "1px solid rgba(212,165,72,0.15)", color: COLORS.cream }}>
              <td style={{ padding: "6px 8px", textAlign: "left" }}>{r.label}</td>
              <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                {fmtInt(r.count)}
              </td>
              <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                {fmtPct(r.freq)}
              </td>
              <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                {r.count > 0 ? r.avg_wager.toFixed(2) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SliceTable({ title, rows, keyLabel }) {
  return (
    <div className="panel" style={{ padding: 12, overflowX: "auto", marginBottom: 14 }}>
      <div style={{ fontSize: 11, color: COLORS.creamDim, marginBottom: 8, letterSpacing: "0.1em" }}>
        {title.toUpperCase()}
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr style={{ color: COLORS.creamDim, textAlign: "right" }}>
            <th style={{ textAlign: "left", padding: "6px 8px" }}>{keyLabel}</th>
            <th style={{ padding: "6px 8px" }}>Plays</th>
            <th style={{ padding: "6px 8px" }}>W-L-P</th>
            <th style={{ padding: "6px 8px" }}>Wagered</th>
            <th style={{ padding: "6px 8px" }}>Net</th>
            <th style={{ padding: "6px 8px" }}>RTP</th>
            <th style={{ padding: "6px 8px" }}>EV / credit</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const evColor =
              r.ev_per_credit === null || r.ev_per_credit === undefined
                ? COLORS.creamDim
                : r.ev_per_credit > 0
                  ? COLORS.goldBright
                  : r.ev_per_credit < 0
                    ? COLORS.red
                    : COLORS.cream;
            const netColor = r.net > 0 ? COLORS.goldBright : r.net < 0 ? COLORS.red : COLORS.cream;
            return (
              <tr key={r.key} style={{ borderTop: "1px solid rgba(212,165,72,0.15)", color: COLORS.cream }}>
                <td style={{ padding: "6px 8px", textAlign: "left" }}>{r.label}</td>
                <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                  {fmtInt(r.count)}
                </td>
                <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                  {fmtInt(r.wins)}-{fmtInt(r.losses)}-{fmtInt(r.pushes)}
                </td>
                <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                  {fmtInt(r.wagered)}
                </td>
                <td style={{ padding: "6px 8px", textAlign: "right", color: netColor }} className="mono">
                  {r.net > 0 ? "+" : ""}
                  {fmtInt(r.net)}
                </td>
                <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                  {fmtRtp(r.rtp)}
                </td>
                <td style={{ padding: "6px 8px", textAlign: "right", color: evColor }} className="mono">
                  {fmtEv(r.ev_per_credit)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function BucketTable({ buckets }) {
  return (
    <div className="panel" style={{ padding: 12, overflowX: "auto", marginBottom: 14 }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr style={{ color: COLORS.creamDim, textAlign: "right" }}>
            <th style={{ textAlign: "left", padding: "6px 8px" }}>Wager</th>
            <th style={{ padding: "6px 8px" }}>Plays</th>
            <th style={{ padding: "6px 8px" }}>Wagered</th>
            <th style={{ padding: "6px 8px" }}>Returned</th>
            <th style={{ padding: "6px 8px" }}>Net</th>
            <th style={{ padding: "6px 8px" }}>Win rate</th>
            <th style={{ padding: "6px 8px" }}>Emp. RTP</th>
            <th style={{ padding: "6px 8px" }}>Theor. RTP</th>
            <th style={{ padding: "6px 8px" }}>EV / credit</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((b) => (
            <tr key={b.key} style={{ borderTop: "1px solid rgba(212,165,72,0.15)", color: COLORS.cream }}>
              <td style={{ padding: "6px 8px", textAlign: "left" }}>{b.label}</td>
              <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                {fmtInt(b.count)}
              </td>
              <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                {fmtInt(b.wagered)}
              </td>
              <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                {fmtInt(b.returned)}
              </td>
              <td
                style={{
                  padding: "6px 8px",
                  textAlign: "right",
                  color: b.net > 0 ? COLORS.goldBright : b.net < 0 ? COLORS.red : COLORS.cream,
                }}
                className="mono"
              >
                {b.net > 0 ? "+" : ""}
                {fmtInt(b.net)}
              </td>
              <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                {fmtPct(b.payout_rate)}
                {b.theoretical_payout_rate !== null && b.theoretical_payout_rate !== undefined && (
                  <span style={{ color: COLORS.creamDim }}> / {fmtPct(b.theoretical_payout_rate)}</span>
                )}
              </td>
              <td style={{ padding: "6px 8px", textAlign: "right" }} className="mono">
                {fmtRtp(b.rtp)}
              </td>
              <td style={{ padding: "6px 8px", textAlign: "right", color: COLORS.creamDim }} className="mono">
                {fmtRtp(b.theoretical_rtp)}
              </td>
              <td
                style={{
                  padding: "6px 8px",
                  textAlign: "right",
                  color:
                    b.ev_per_credit === null || b.ev_per_credit === undefined
                      ? COLORS.creamDim
                      : b.ev_per_credit > 0
                        ? COLORS.goldBright
                        : b.ev_per_credit < 0
                          ? COLORS.red
                          : COLORS.cream,
                }}
                className="mono"
              >
                {fmtEv(b.ev_per_credit)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TimelineTable({ timeline }) {
  const maxAbsNet = Math.max(1, ...timeline.map((t) => Math.abs(t.net)));
  return (
    <div className="panel" style={{ padding: 12, overflowX: "auto" }}>
      <div style={{ fontSize: 11, color: COLORS.creamDim, marginBottom: 8, letterSpacing: "0.1em" }}>BY DAY (UTC)</div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr style={{ color: COLORS.creamDim, textAlign: "right" }}>
            <th style={{ textAlign: "left", padding: "4px 8px" }}>Date</th>
            <th style={{ padding: "4px 8px" }}>Plays</th>
            <th style={{ padding: "4px 8px" }}>Wagered</th>
            <th style={{ padding: "4px 8px" }}>Returned</th>
            <th style={{ padding: "4px 8px" }}>Net</th>
            <th style={{ padding: "4px 8px" }}>RTP</th>
            <th style={{ textAlign: "left", padding: "4px 8px", minWidth: 160 }}>Net (visual)</th>
          </tr>
        </thead>
        <tbody>
          {timeline.map((row) => {
            const pct = (Math.abs(row.net) / maxAbsNet) * 100;
            const positive = row.net >= 0;
            return (
              <tr key={row.date} style={{ borderTop: "1px solid rgba(212,165,72,0.1)", color: COLORS.cream }}>
                <td style={{ padding: "4px 8px", textAlign: "left" }} className="mono">
                  {row.date}
                </td>
                <td style={{ padding: "4px 8px", textAlign: "right" }} className="mono">
                  {fmtInt(row.count)}
                </td>
                <td style={{ padding: "4px 8px", textAlign: "right" }} className="mono">
                  {fmtInt(row.wagered)}
                </td>
                <td style={{ padding: "4px 8px", textAlign: "right" }} className="mono">
                  {fmtInt(row.returned)}
                </td>
                <td
                  style={{
                    padding: "4px 8px",
                    textAlign: "right",
                    color: row.net > 0 ? COLORS.goldBright : row.net < 0 ? COLORS.red : COLORS.cream,
                  }}
                  className="mono"
                >
                  {row.net > 0 ? "+" : ""}
                  {fmtInt(row.net)}
                </td>
                <td style={{ padding: "4px 8px", textAlign: "right" }} className="mono">
                  {fmtRtp(row.rtp)}
                </td>
                <td style={{ padding: "4px 8px" }}>
                  <div style={{ height: 6, background: "rgba(0,0,0,0.4)", borderRadius: 2, position: "relative" }}>
                    <div
                      style={{
                        height: "100%",
                        width: `${pct}%`,
                        background: positive ? COLORS.gold : COLORS.red,
                      }}
                    />
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
