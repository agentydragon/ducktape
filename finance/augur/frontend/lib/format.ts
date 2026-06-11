export function fmtUsd(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return number.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

export function fmtUsdCompact(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return number.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: Math.abs(number) >= 1_000_000 ? 2 : 1,
  });
}

export function fmtPct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return `${(number * 100).toFixed(1)}%`;
}

export function fmtNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return number.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

export function fmtQuantity(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return number.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

// Compact platform-volume formatter: "USD" -> "$42k" (built-in compact-currency notation),
// "contracts" -> "44k contracts" (Kalshi binary contracts resolve $0–$1, so this is a
// bounded-above proxy for dollar volume), anything else (e.g. Manifold's "𝕄" mana) is
// rendered as `<unit><compact-number>`.
export function fmtVolume(amount, unit) {
  const number = Number(amount);
  if (!Number.isFinite(number) || !unit) return null;
  if (unit === "USD") return fmtUsdCompact(number);
  const compact = number.toLocaleString("en-US", {
    notation: "compact",
    maximumFractionDigits: Math.abs(number) >= 1_000_000 ? 2 : 1,
  });
  if (unit === "contracts") return `${compact} contracts`;
  return `${unit}${compact}`;
}

export function clampInteger(value, min, max) {
  const number = Math.trunc(Number(value));
  if (!Number.isFinite(number)) return min;
  return Math.min(max, Math.max(min, number));
}
