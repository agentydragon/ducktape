import type { ReactNode } from "react";

/** A labelled value in a preview/detail view. The default stacks a small uppercase text label over
 * the value; passing `icon` swaps that label for an inline icon on the value's own row, saving the
 * label row for short values — the label rides along as the icon's tooltip/aria-label. */
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
}): JSX.Element {
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
