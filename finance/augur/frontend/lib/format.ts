export function fmtUsd(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return number.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

export function fmtUsdCompact(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return number.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: Math.abs(number) >= 1_000_000 ? 2 : 1,
  });
}

function parseQuantum(quantum: unknown) {
  const text = String(quantum ?? "");
  if (!/^\d+(?:\.\d+)?$/.test(text) || /^0(?:\.0+)?$/.test(text)) return null;
  const [whole, fraction = ""] = text.split(".");
  return { scaled: BigInt(`${whole}${fraction}`), scale: fraction.length };
}

function groupedInteger(value: bigint) {
  const text = value.toString();
  return text.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Exact decimal rendering of a base-10 integer currency quantum count.
 *
 * Never sends a quantum through JavaScript Number: responses can legitimately
 * contain the full signed Int64 range. `currencyQuantum` is an exact decimal
 * such as "0.01", "1", or "0.05" supplied by the product response.
 */
export function currencyQuantaDecimal(value: unknown, currencyQuantum: unknown) {
  if (typeof value !== "string" && typeof value !== "bigint" && typeof value !== "number") return null;
  let quanta;
  try {
    quanta = BigInt(value);
  } catch {
    return null;
  }
  const quantum = parseQuantum(currencyQuantum);
  if (!quantum) return null;
  const negative = quanta < 0n;
  const scaled = (negative ? -quanta : quanta) * quantum.scaled;
  const denominator = 10n ** BigInt(quantum.scale);
  const whole = scaled / denominator;
  const fraction = (scaled % denominator).toString().padStart(quantum.scale, "0");
  const decimal = quantum.scale === 0 ? groupedInteger(whole) : `${groupedInteger(whole)}.${fraction}`;
  return `${negative ? "-" : ""}${decimal}`;
}

export function fmtQuanta(
  value: unknown,
  { currencyCode = "USD", currencyQuantum = "0.01" }: { currencyCode?: string; currencyQuantum?: unknown } = {}
) {
  const decimal = currencyQuantaDecimal(value, currencyQuantum);
  if (decimal == null) return "n/a";
  // Currency symbols and their placement are locale-specific. Keep the ISO code
  // explicit so a non-USD product response is never displayed as dollars.
  return `${currencyCode}\u00a0${decimal}`;
}

export function fmtChartCurrency(value: unknown, { currencyCode = "USD" }: { currencyCode?: string } = {}) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return `${currencyCode}\u00a0${number.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

// Charts are the one allowed Number conversion: SVG coordinates cannot consume
// BigInt. Labels, tooltips, tables, and event text use fmtQuanta above.
export function currencyQuantaChartNumber(value: unknown, currencyQuantum: unknown) {
  const quanta = Number(value);
  const quantum = Number(currencyQuantum);
  return Number.isFinite(quanta) && Number.isFinite(quantum) && quantum > 0 ? quanta * quantum : NaN;
}

function parseQuanta(value: unknown): bigint | null {
  try {
    // Preserve BigInt's runtime coercion for inputs outside the currency schemas too; callers
    // already treated conversion errors as invalid amounts.
    return BigInt(value as string | number | bigint | boolean);
  } catch {
    return null;
  }
}

export function currencyQuantaIsPositive(value: unknown) {
  return (parseQuanta(value) ?? 0n) > 0n;
}

export function currencyQuantaCompare(left: unknown, right: unknown) {
  const leftQuanta = parseQuanta(left);
  const rightQuanta = parseQuanta(right);
  if (leftQuanta == null || rightQuanta == null) return NaN;
  const difference = leftQuanta - rightQuanta;
  return difference < 0n ? -1 : difference > 0n ? 1 : 0;
}

export function currencyQuantaAdd(...values: unknown[]) {
  let sum = 0n;
  for (const value of values) {
    const quanta = parseQuanta(value);
    if (quanta == null) return null;
    sum += quanta;
  }
  return sum.toString();
}

export function fmtPct(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return `${(number * 100).toFixed(1)}%`;
}

export function fmtNumber(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return number.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

export function fmtQuantity(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return number.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

export function fmtVolume(amount: unknown, unit: string | null | undefined) {
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

export function clampInteger(value: unknown, min: number, max: number) {
  const number = Math.trunc(Number(value));
  if (!Number.isFinite(number)) return min;
  return Math.min(max, Math.max(min, number));
}
