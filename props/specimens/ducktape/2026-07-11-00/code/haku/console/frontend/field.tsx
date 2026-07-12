import type { ReactNode } from "react";

/** A labelled value in a preview/detail view. Both forms share one wrapper and value element —
 * only the label differs: the default stacks a small uppercase text label over the value, while
 * passing `icon` swaps that label for an inline icon on the value's own row (the label rides
 * along as the icon's tooltip/aria-label), saving the whole label row for short values. */
export function Field({
  label,
  children,
  mono = false,
  icon,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
  icon?: ReactNode;
}) {
  return (
    <div className={`haku-shell-field ${icon ? "haku-shell-field-inline" : ""}`}>
      {icon ? (
        <span className="haku-shell-field-icon" role="img" aria-label={label} title={label}>
          {icon}
        </span>
      ) : (
        <span className="haku-shell-field-label">{label}</span>
      )}
      <div className={`haku-shell-field-value ${mono ? "haku-shell-mono" : ""}`}>{children}</div>
    </div>
  );
}
