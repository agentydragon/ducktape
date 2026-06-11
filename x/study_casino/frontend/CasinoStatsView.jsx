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
const fmtCreditDelta = (v) => {
  if (v === null || v === undefined) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}`;
};
const fmtInt = (n) => (n ?? 0).toLocaleString();

const TABLE_PANEL_STYLE = { padding: 12, overflowX: "auto", marginBottom: 14 };
const TABLE_STYLE = { width: "100%", borderCollapse: "collapse", fontSize: 12 };
const TABLE_TITLE_STYLE = { fontSize: 11, color: COLORS.creamDim, marginBottom: 8, letterSpacing: "0.1em" };
const TABLE_HEADER_ROW_STYLE = { color: COLORS.creamDim, textAlign: "right" };
const TABLE_ROW_STYLE = { borderTop: "1px solid rgba(212,165,72,0.15)", color: COLORS.cream };
const TABLE_SUBTLE_ROW_STYLE = { borderTop: "1px solid rgba(212,165,72,0.1)", color: COLORS.cream };

const valueColor = (value, zero = COLORS.cream, empty = COLORS.creamDim) => {
  if (value === null || value === undefined) return empty;
  if (value > 0) return COLORS.goldBright;
  if (value < 0) return COLORS.red;
  return zero;
};

const signedInt = (value) => `${value > 0 ? "+" : ""}${fmtInt(value)}`;

function TablePanel({ title, children, style }) {
  return (
    <div className="panel" style={{ ...TABLE_PANEL_STYLE, ...style }}>
      {title && <div style={TABLE_TITLE_STYLE}>{title.toUpperCase()}</div>}
      <table style={TABLE_STYLE}>{children}</table>
    </div>
  );
}

function HeaderCell({ children, align = "right", compact = false, style, title }) {
  return (
    <th title={title} style={{ textAlign: align, padding: compact ? "4px 8px" : "6px 8px", ...style }}>
      {children}
    </th>
  );
}

function Row({ children, subtle = false }) {
  return <tr style={subtle ? TABLE_SUBTLE_ROW_STYLE : TABLE_ROW_STYLE}>{children}</tr>;
}

function Cell({ children, align = "right", color, mono = true, compact = false, style }) {
  return (
    <td
      className={mono ? "mono" : undefined}
      style={{ padding: compact ? "4px 8px" : "6px 8px", textAlign: align, color, ...style }}
    >
      {children}
    </td>
  );
}

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
        <span className="mono">returned / wagered</span>. Fair values are rule expectations for the wagers actually
        placed, not an equal-choice roulette strategy.
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
          <BucketTable buckets={[game.total, ...game.buckets]} showLuck={game.game === "roulette"} />
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
  const netColor = valueColor(total.net, COLORS.cream);
  const evColor = valueColor(total.ev_per_credit, COLORS.cream);
  return (
    <div className="panel" style={{ padding: 14, marginBottom: 14, display: "flex", flexWrap: "wrap", gap: 22 }}>
      {stat("Plays", fmtInt(summary.count))}
      {stat("W / L / P", `${fmtInt(summary.wins)} / ${fmtInt(summary.losses)} / ${fmtInt(summary.pushes)}`)}
      {stat("Win rate (excl. push)", fmtPct(summary.win_rate_excl_push))}
      {stat("Blackjack rate", fmtPct(summary.blackjack_rate))}
      {stat("Wagered", fmtInt(total.wagered))}
      {stat("Returned", fmtInt(total.returned))}
      {stat("Net", signedInt(total.net), netColor)}
      {stat("Emp. RTP", fmtRtp(total.rtp))}
      {stat("EV / credit", fmtEv(total.ev_per_credit), evColor)}
    </div>
  );
}

function OutcomeFreqTable({ rows }) {
  return (
    <TablePanel title="Outcome frequency">
      <thead>
        <tr style={TABLE_HEADER_ROW_STYLE}>
          <HeaderCell align="left">Outcome</HeaderCell>
          <HeaderCell>Plays</HeaderCell>
          <HeaderCell>Frequency</HeaderCell>
          <HeaderCell>Avg wager</HeaderCell>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <Row key={r.key}>
            <Cell align="left" mono={false}>
              {r.label}
            </Cell>
            <Cell>{fmtInt(r.count)}</Cell>
            <Cell>{fmtPct(r.freq)}</Cell>
            <Cell>{r.count > 0 ? r.avg_wager.toFixed(2) : "—"}</Cell>
          </Row>
        ))}
      </tbody>
    </TablePanel>
  );
}

function SliceTable({ title, rows, keyLabel }) {
  return (
    <TablePanel title={title}>
      <thead>
        <tr style={TABLE_HEADER_ROW_STYLE}>
          <HeaderCell align="left">{keyLabel}</HeaderCell>
          <HeaderCell>Plays</HeaderCell>
          <HeaderCell>W-L-P</HeaderCell>
          <HeaderCell>Wagered</HeaderCell>
          <HeaderCell>Net</HeaderCell>
          <HeaderCell>RTP</HeaderCell>
          <HeaderCell>EV / credit</HeaderCell>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const evColor = valueColor(r.ev_per_credit, COLORS.cream);
          return (
            <Row key={r.key}>
              <Cell align="left" mono={false}>
                {r.label}
              </Cell>
              <Cell>{fmtInt(r.count)}</Cell>
              <Cell>
                {fmtInt(r.wins)}-{fmtInt(r.losses)}-{fmtInt(r.pushes)}
              </Cell>
              <Cell>{fmtInt(r.wagered)}</Cell>
              <Cell color={valueColor(r.net, COLORS.cream)}>{signedInt(r.net)}</Cell>
              <Cell>{fmtRtp(r.rtp)}</Cell>
              <Cell color={evColor}>{fmtEv(r.ev_per_credit)}</Cell>
            </Row>
          );
        })}
      </tbody>
    </TablePanel>
  );
}

function BucketTable({ buckets, showLuck = false }) {
  return (
    <TablePanel>
      <thead>
        <tr style={TABLE_HEADER_ROW_STYLE}>
          <HeaderCell align="left">Wager</HeaderCell>
          <HeaderCell>Plays</HeaderCell>
          <HeaderCell>Wagered</HeaderCell>
          <HeaderCell>Returned</HeaderCell>
          <HeaderCell>Obs. net</HeaderCell>
          <HeaderCell>Fair net</HeaderCell>
          <HeaderCell>Win rate</HeaderCell>
          <HeaderCell>RTP</HeaderCell>
          <HeaderCell>EV / cr</HeaderCell>
          {showLuck && (
            <HeaderCell title="Probability that fair roulette would produce this many wins or fewer for these wager types. Wager sizes are ignored.">
              Fair P&lt;=wins
            </HeaderCell>
          )}
        </tr>
      </thead>
      <tbody>
        {buckets.map((b) => {
          const evColor = valueColor(b.ev_per_credit, COLORS.cream);
          const fairNetColor = valueColor(b.expected_net, COLORS.creamDim);
          return (
            <Row key={b.key}>
              <Cell align="left" mono={false}>
                {b.label}
              </Cell>
              <Cell>{fmtInt(b.count)}</Cell>
              <Cell>{fmtInt(b.wagered)}</Cell>
              <Cell>{fmtInt(b.returned)}</Cell>
              <Cell color={valueColor(b.net, COLORS.cream)}>{signedInt(b.net)}</Cell>
              <Cell color={fairNetColor}>{fmtCreditDelta(b.expected_net)}</Cell>
              <Cell>
                {fmtPct(b.payout_rate)}
                {b.theoretical_payout_rate !== null && b.theoretical_payout_rate !== undefined && (
                  <span style={{ color: COLORS.creamDim }}> / {fmtPct(b.theoretical_payout_rate)}</span>
                )}
              </Cell>
              <Cell>
                {fmtRtp(b.rtp)}
                {b.theoretical_rtp !== null && b.theoretical_rtp !== undefined && (
                  <span style={{ color: COLORS.creamDim }}> / {fmtRtp(b.theoretical_rtp)}</span>
                )}
              </Cell>
              <Cell color={evColor}>
                {fmtEv(b.ev_per_credit)}
                {b.theoretical_ev_per_credit !== null && b.theoretical_ev_per_credit !== undefined && (
                  <span style={{ color: COLORS.creamDim }}> / {fmtEv(b.theoretical_ev_per_credit)}</span>
                )}
              </Cell>
              {showLuck && <Cell>{fmtPct(b.fair_win_lower_tail_probability)}</Cell>}
            </Row>
          );
        })}
      </tbody>
    </TablePanel>
  );
}

function TimelineTable({ timeline }) {
  const maxAbsNet = Math.max(1, ...timeline.map((t) => Math.abs(t.net)));
  return (
    <TablePanel title="By day (UTC)" style={{ marginBottom: 0 }}>
      <thead>
        <tr style={TABLE_HEADER_ROW_STYLE}>
          <HeaderCell align="left" compact>
            Date
          </HeaderCell>
          <HeaderCell compact>Plays</HeaderCell>
          <HeaderCell compact>Wagered</HeaderCell>
          <HeaderCell compact>Returned</HeaderCell>
          <HeaderCell compact>Net</HeaderCell>
          <HeaderCell compact>RTP</HeaderCell>
          <HeaderCell align="left" compact style={{ minWidth: 160 }}>
            Net (visual)
          </HeaderCell>
        </tr>
      </thead>
      <tbody>
        {timeline.map((row) => {
          const pct = (Math.abs(row.net) / maxAbsNet) * 100;
          const positive = row.net >= 0;
          return (
            <Row key={row.date} subtle>
              <Cell align="left" compact>
                {row.date}
              </Cell>
              <Cell compact>{fmtInt(row.count)}</Cell>
              <Cell compact>{fmtInt(row.wagered)}</Cell>
              <Cell compact>{fmtInt(row.returned)}</Cell>
              <Cell compact color={valueColor(row.net, COLORS.cream)}>
                {signedInt(row.net)}
              </Cell>
              <Cell compact>{fmtRtp(row.rtp)}</Cell>
              <Cell compact align="left" mono={false}>
                <div style={{ height: 6, background: "rgba(0,0,0,0.4)", borderRadius: 2, position: "relative" }}>
                  <div
                    style={{
                      height: "100%",
                      width: `${pct}%`,
                      background: positive ? COLORS.gold : COLORS.red,
                    }}
                  />
                </div>
              </Cell>
            </Row>
          );
        })}
      </tbody>
    </TablePanel>
  );
}
