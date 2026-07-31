import React from "react";
import { AugurHeader } from "./header";

function Skeleton({ className, style }: { className: string; style?: React.CSSProperties }) {
  return <div className={`animate-pulse rounded bg-slate-200 dark:bg-slate-800 ${className}`} style={style} />;
}

export function RolloutResultsSkeleton() {
  return (
    <section className="augur-panel overflow-hidden" aria-label="Cash projection workspace">
      <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <Skeleton className="h-8 w-48" />
      </div>
      <div className="px-4 py-3">
        <Skeleton className="mb-2 h-4 w-56" />
        <Skeleton className="h-4 w-40" />
        <div className="mt-3 flex items-end gap-px" style={{ height: 160 }}>
          {Array.from({ length: 24 }, (_, i) => (
            <Skeleton key={i} className="flex-1" style={{ height: `${30 + Math.random() * 70}%` }} />
          ))}
        </div>
      </div>
      <div className="p-4">
        <Skeleton className="h-[22rem] w-full rounded" />
      </div>
    </section>
  );
}

export function StatCardsSkeleton() {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {Array.from({ length: 2 }, (_, i) => (
        <div key={i} className="augur-card p-4">
          <Skeleton className="mb-2 h-3 w-32" />
          <Skeleton className="h-8 w-28" />
        </div>
      ))}
    </div>
  );
}

export function ProductProjectionLoading({ error }) {
  return (
    <div
      data-augur-surface="product"
      className="min-h-screen bg-slate-100 text-slate-800 dark:bg-slate-950 dark:text-slate-100"
    >
      <AugurHeader rightSlot={<span className="whitespace-nowrap">Product API</span>} />
      <main className="px-4 py-6 sm:px-6 lg:px-8">
        {error ? (
          <div className="augur-note-danger max-w-lg p-4 text-sm">Augur bootstrap failed: {error}</div>
        ) : (
          <div className="augur-card max-w-lg p-4 text-sm augur-muted">Loading...</div>
        )}
      </main>
    </div>
  );
}
