import React, { useEffect, useMemo, useState } from "react";
import { NativeSelect } from "@mantine/core";

import { fetchBudgetSnapshot, fetchBudgetTransactions } from "./client.ts";
import { fmtUsd, fmtNumber } from "./lib/format.ts";

// Buckets the user "consumes" cash through. Reimbursable rows already net against
// their paired reimbursement bucket on the server, so we treat them like expenses here.
const SPEND_KINDS = new Set(["expense", "reimbursable"]);

const WINDOW_CHOICES = [
  { value: "3", label: "Trailing 3 months" },
  { value: "6", label: "Trailing 6 months" },
  { value: "12", label: "Trailing 12 months" },
  { value: "24", label: "Trailing 24 months" },
];

function fmtMonth(iso) {
  // iso: "YYYY-MM-DD" — render as "Jul '25" so 12 months fit on a row of pills.
  const [yearStr, monthStr] = iso.split("-");
  const month = Number(monthStr);
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${names[month - 1]} '${yearStr.slice(2)}`;
}

function KindBadge({ kind }) {
  const tone =
    {
      expense: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
      reimbursable: "bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-300",
      reimbursement: "bg-sky-50 text-sky-800 dark:bg-sky-950/40 dark:text-sky-300",
      transfer: "bg-slate-50 text-slate-500 italic dark:bg-slate-900 dark:text-slate-500",
      income: "bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300",
    }[kind] || "bg-slate-100 text-slate-700";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${tone}`}
    >
      {kind}
    </span>
  );
}

function Sparkline({ amounts, width = 90, height = 24 }) {
  if (!amounts.length) return null;
  // Plaid sign convention: + = money out. For expense buckets we display + as up.
  // Reimbursement / income buckets read as negative outflow — flip so they show as positive bars.
  const values = amounts;
  const max = Math.max(...values.map((value) => Math.abs(value)));
  if (max === 0) return <svg width={width} height={height} aria-hidden="true" />;
  const step = width / Math.max(values.length - 1, 1);
  const points = values
    .map((value, index) => {
      const x = index * step;
      const y = height / 2 - (value / max) * (height / 2 - 1);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <line x1="0" x2={width} y1={height / 2} y2={height / 2} stroke="currentColor" strokeOpacity="0.15" />
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

// "Nice" tick values for a chart axis: pick a step that's a 1/2/5 × 10^n round number
// and brackets the data, so axis labels read $5k/$10k/$15k rather than $4,237/$8,474.
function niceTicks(max: number, target = 5): { ticks: number[]; ceiling: number } {
  if (max <= 0) return { ticks: [0], ceiling: 1 };
  const rawStep = max / target;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const candidates = [1, 2, 5, 10].map((m) => m * magnitude);
  const step = candidates.find((c) => c >= rawStep) ?? candidates[candidates.length - 1];
  const ceiling = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let t = 0; t <= ceiling + 1e-9; t += step) ticks.push(t);
  return { ticks, ceiling };
}

function StackedMonthlyChart({ months, bucketSeries }) {
  // Stacked column per month. Only spend buckets (EXPENSE + REIMBURSABLE) contribute to the
  // outflow stack; income / transfers shown elsewhere.
  const spendSeries = bucketSeries.filter((series) => SPEND_KINDS.has(series.kind));
  if (!months.length || !spendSeries.length) {
    return <div className="px-4 py-6 text-sm augur-muted">No spend data in window.</div>;
  }
  const monthlyTotals = months.map((_, i) =>
    spendSeries.reduce((acc, series) => acc + Math.max(series.monthlyAmounts[i], 0), 0)
  );
  const max = Math.max(...monthlyTotals);
  const { ticks, ceiling } = niceTicks(max);
  const palette = [
    "#0ea5e9",
    "#f97316",
    "#10b981",
    "#a855f7",
    "#ef4444",
    "#eab308",
    "#06b6d4",
    "#ec4899",
    "#84cc16",
    "#6366f1",
    "#14b8a6",
    "#f43f5e",
    "#22d3ee",
    "#fb923c",
    "#a3e635",
    "#c084fc",
    "#fbbf24",
    "#94a3b8",
  ];
  // Reserve left margin (px) for the y-axis labels + right padding. The bars live in
  // an inner viewBox; preserveAspectRatio=none stretches them horizontally to fit.
  const yAxisWidthPx = 56;
  const innerHeightPx = 220;
  return (
    <div className="overflow-hidden">
      <div className="flex" style={{ height: innerHeightPx }}>
        <div
          className="flex flex-col justify-between text-right text-[10px] augur-muted"
          style={{ width: yAxisWidthPx, paddingRight: 6 }}
        >
          {ticks
            .slice()
            .reverse()
            .map((value) => (
              <span key={value} className="leading-none augur-tabular">
                {fmtUsd(value)}
              </span>
            ))}
        </div>
        <div className="relative flex-1">
          {/* Horizontal gridlines per tick, drawn under the bars. */}
          <div className="absolute inset-0 flex flex-col justify-between">
            {ticks
              .slice()
              .reverse()
              .map((value) => (
                <div key={value} className="border-t border-slate-200 dark:border-slate-700/60" style={{ height: 0 }} />
              ))}
          </div>
          <svg
            viewBox={`0 0 100 ${ceiling}`}
            preserveAspectRatio="none"
            width="100%"
            height={innerHeightPx}
            role="img"
            aria-label="Monthly stacked spend"
          >
            {months.map((monthIso, monthIdx) => {
              const barWidth = 100 / months.length;
              let cursor = ceiling;
              const segments = spendSeries.map((series, seriesIdx) => {
                const value = Math.max(series.monthlyAmounts[monthIdx], 0);
                if (value <= 0) return null;
                const y = cursor - value;
                cursor = y;
                return (
                  <rect
                    key={series.bucketId}
                    x={monthIdx * barWidth + barWidth * 0.1}
                    y={y}
                    width={barWidth * 0.8}
                    height={value}
                    fill={palette[seriesIdx % palette.length]}
                    opacity="0.9"
                  >
                    <title>{`${series.label} · ${fmtMonth(monthIso)}: ${fmtUsd(value)}`}</title>
                  </rect>
                );
              });
              return <g key={monthIso}>{segments}</g>;
            })}
          </svg>
        </div>
      </div>
      <div className="flex text-[10px] augur-muted" style={{ paddingLeft: yAxisWidthPx }}>
        {months.map((monthIso) => (
          <span key={monthIso} className="flex-1 text-center">
            {fmtMonth(monthIso)}
          </span>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[11px]" style={{ paddingLeft: yAxisWidthPx }}>
        {spendSeries.map((series, idx) => (
          <span key={series.bucketId} className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: palette[idx % palette.length] }} />
            {series.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function BucketRow({ entry, onSelect, selected }) {
  const trend = entry.monthlyAmounts;
  return (
    <tr
      className={`cursor-pointer transition-colors ${selected ? "bg-sky-50 dark:bg-sky-950/30" : "hover:bg-slate-50 dark:hover:bg-slate-900"}`}
      onClick={() => onSelect(entry.bucketId)}
      data-budget-bucket-row={entry.bucketId}
    >
      <th className="px-3 py-2 text-left text-sm font-semibold text-slate-700 dark:text-slate-200">
        {entry.label}
        <span className="ml-2">
          <KindBadge kind={entry.kind} />
        </span>
      </th>
      <td className="px-3 py-2 text-right text-sm augur-tabular">{fmtUsd(entry.currentMonthlyAvg)}</td>
      <td className="px-3 py-2 text-right text-sm augur-tabular augur-muted">{fmtNumber(entry.transactionCount)}</td>
      <td className="px-3 py-2 text-right">
        <Sparkline amounts={trend} />
      </td>
    </tr>
  );
}

function LumpyPanel({ items, bucketsById }) {
  if (!items.length) {
    return <div className="px-4 py-6 text-sm augur-muted">No lumpy spends in window above threshold.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full border-t border-slate-200 text-sm dark:border-slate-700">
        <thead>
          <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            <th className="px-3 py-2 font-semibold">Date</th>
            <th className="px-3 py-2 font-semibold">Merchant</th>
            <th className="px-3 py-2 font-semibold">Bucket</th>
            <th className="px-3 py-2 text-right font-semibold">Amount</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {items.map((item) => (
            <tr key={item.transactionId} data-budget-lumpy-row={item.transactionId}>
              <td className="px-3 py-2 augur-tabular text-xs">{item.date}</td>
              <th className="px-3 py-2 text-left font-medium text-slate-700 dark:text-slate-200">
                {item.merchantName || item.name}
              </th>
              <td className="px-3 py-2 text-xs">{bucketsById.get(item.bucketId)?.label || item.bucketId}</td>
              <td className="px-3 py-2 text-right augur-tabular">{fmtUsd(item.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TransactionsPanel({ transactions }) {
  if (!transactions) return <div className="px-4 py-6 text-sm augur-muted">Loading…</div>;
  if (!transactions.length) {
    return <div className="px-4 py-6 text-sm augur-muted">No transactions in this bucket.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            <th className="px-3 py-2 font-semibold">Date</th>
            <th className="px-3 py-2 font-semibold">Merchant / Descriptor</th>
            <th className="px-3 py-2 font-semibold">Plaid PFC</th>
            <th className="px-3 py-2 font-semibold">Account</th>
            <th className="px-3 py-2 text-right font-semibold">Amount</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {transactions.map((tx) => (
            <tr key={tx.transactionId}>
              <td className="px-3 py-2 augur-tabular text-xs">{tx.date}</td>
              <th className="px-3 py-2 text-left text-slate-700 dark:text-slate-200">
                <div className="font-medium">{tx.merchantName || tx.name}</div>
                {tx.merchantName && tx.merchantName !== tx.name && (
                  <div className="text-[10px] augur-muted truncate max-w-md">{tx.name}</div>
                )}
              </th>
              <td className="px-3 py-2 text-[11px] augur-muted">{tx.pfcDetailed || tx.pfcPrimary || "—"}</td>
              <td className="px-3 py-2 text-xs">{tx.accountName}</td>
              <td className="px-3 py-2 text-right augur-tabular">{fmtUsd(tx.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function BudgetWorkspace() {
  const [months, setMonths] = useState(12);
  const [snapshot, setSnapshot] = useState(null);
  const [snapshotError, setSnapshotError] = useState(null);
  const [selectedBucketId, setSelectedBucketId] = useState(null);
  const [bucketTx, setBucketTx] = useState(null);
  const [bucketTxError, setBucketTxError] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    setSnapshot(null);
    setSnapshotError(null);
    fetchBudgetSnapshot({ months }, { signal: controller.signal })
      .then((payload) => setSnapshot(payload))
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setSnapshotError(error?.message || String(error));
      });
    return () => controller.abort();
  }, [months]);

  useEffect(() => {
    if (!selectedBucketId) return undefined;
    const controller = new AbortController();
    setBucketTx(null);
    setBucketTxError(null);
    fetchBudgetTransactions({ bucketId: selectedBucketId, months }, { signal: controller.signal })
      .then((payload) => setBucketTx(payload.transactions))
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setBucketTxError(error?.message || String(error));
      });
    return () => controller.abort();
  }, [selectedBucketId, months]);

  const bucketsById = useMemo(() => {
    const out = new Map();
    if (snapshot) for (const bucket of snapshot.buckets) out.set(bucket.id, bucket);
    return out;
  }, [snapshot]);

  const rows = useMemo(() => {
    if (!snapshot) return [];
    return snapshot.monthlyByBucket
      .map((series) => {
        const bucket = bucketsById.get(series.bucketId);
        return {
          bucketId: series.bucketId,
          label: bucket?.label ?? series.bucketId,
          kind: bucket?.kind ?? "expense",
          monthlyAmounts: series.monthlyAmounts,
          currentMonthlyAvg: series.currentMonthlyAvg,
          transactionCount: series.transactionCount,
        };
      })
      .sort((left, right) => Math.abs(right.currentMonthlyAvg) - Math.abs(left.currentMonthlyAvg));
  }, [snapshot, bucketsById]);

  const totals = useMemo(() => {
    if (!snapshot) return null;
    let spend = 0;
    let incomeFlow = 0;
    for (const row of rows) {
      if (SPEND_KINDS.has(row.kind)) spend += row.currentMonthlyAvg;
      if (row.kind === "income") incomeFlow += row.currentMonthlyAvg;
    }
    return { spend, incomeFlow };
  }, [snapshot, rows]);

  return (
    <main className="px-4 py-6 sm:px-6 lg:px-8 space-y-5">
      <section className="augur-panel p-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <div className="augur-eyebrow">Budget planner</div>
            <div className="mt-1 text-xs augur-muted">
              Live spend from Plaid mirror. Reimbursable buckets (e.g. medical) are netted against their paired
              reimbursement stream on a rolling {snapshot ? snapshot.reimbursementWindowMonths : "—"}-month window.
            </div>
          </div>
          <NativeSelect
            aria-label="Window"
            data={WINDOW_CHOICES}
            value={String(months)}
            onChange={(event) => setMonths(Number(event.target.value))}
            classNames={{ input: "augur-tabular min-w-[12rem]" }}
          />
        </div>
        {snapshot?.coverageStarts && (
          <div className="mt-2 text-[11px] augur-muted">
            Spend before <span className="font-semibold">{snapshot.coverageStarts}</span> is partial -- one or more
            linked accounts didn&apos;t return earlier transactions. The selected window is clamped to that date for
            consistency.
          </div>
        )}
      </section>

      {snapshotError && <div className="augur-note-danger p-4 text-sm">Budget snapshot failed: {snapshotError}</div>}

      {!snapshot && !snapshotError && (
        <div className="augur-panel p-8 text-center text-sm augur-muted">Loading budget snapshot…</div>
      )}

      {snapshot && (
        <>
          <section className="grid gap-3 sm:grid-cols-3">
            <div className="augur-card p-4">
              <div className="augur-eyebrow">Monthly spend (avg, 3mo)</div>
              <div className="mt-2 text-2xl font-semibold augur-tabular">{fmtUsd(totals.spend)}</div>
            </div>
            <div className="augur-card p-4">
              <div className="augur-eyebrow">Monthly income (avg, 3mo)</div>
              <div className="mt-2 text-2xl font-semibold augur-tabular text-emerald-700 dark:text-emerald-400">
                {fmtUsd(-totals.incomeFlow)}
              </div>
            </div>
            <div className="augur-card p-4">
              <div className="augur-eyebrow">Net monthly burn</div>
              <div className="mt-2 text-2xl font-semibold augur-tabular">
                {fmtUsd(totals.spend + totals.incomeFlow)}
              </div>
              <div className="text-[11px] augur-muted">Positive = drawing down savings.</div>
            </div>
          </section>

          <section className="augur-panel overflow-hidden">
            <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
              <div className="augur-eyebrow">Monthly spend by bucket</div>
            </div>
            <div className="p-4">
              <StackedMonthlyChart months={snapshot.months} bucketSeries={rows} />
            </div>
          </section>

          <section className="augur-panel overflow-hidden">
            <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
              <div className="augur-eyebrow">Buckets</div>
              <div className="mt-1 text-xs augur-muted">
                Click a row to drill into its transactions. Sorted by recent monthly average (largest first).
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
                    <th className="px-3 py-2 font-semibold">Bucket</th>
                    <th className="px-3 py-2 text-right font-semibold">Recent $/mo</th>
                    <th className="px-3 py-2 text-right font-semibold">Tx count</th>
                    <th className="px-3 py-2 text-right font-semibold">Trend</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {rows.map((row) => (
                    <BucketRow
                      key={row.bucketId}
                      entry={row}
                      onSelect={setSelectedBucketId}
                      selected={selectedBucketId === row.bucketId}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          {selectedBucketId && (
            <section className="augur-panel overflow-hidden">
              <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                <div>
                  <div className="augur-eyebrow">Transactions — {bucketsById.get(selectedBucketId)?.label}</div>
                  <div className="mt-1 text-xs augur-muted">All transactions in the window for this bucket.</div>
                </div>
                <button type="button" className="text-xs augur-link" onClick={() => setSelectedBucketId(null)}>
                  Close
                </button>
              </div>
              {bucketTxError ? (
                <div className="augur-note-danger p-4 text-sm">Transactions failed: {bucketTxError}</div>
              ) : (
                <TransactionsPanel transactions={bucketTx} />
              )}
            </section>
          )}

          <section className="augur-panel overflow-hidden">
            <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
              <div className="augur-eyebrow">Lumpy spends (≥ {fmtUsd(snapshot.lumpyThresholdUsd)})</div>
              <div className="mt-1 text-xs augur-muted">
                Single large outflows in the window — flagged so you can decide whether each is truly one-off vs
                recurring.
              </div>
            </div>
            <LumpyPanel items={snapshot.lumpy} bucketsById={bucketsById} />
          </section>
        </>
      )}
    </main>
  );
}
