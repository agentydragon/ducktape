import type { JSX, ReactNode } from "react";

export function Field({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}): JSX.Element {
  return (
    <div className="haku-shell-field">
      <span className="haku-shell-field-label">{label}</span>
      <div className={`haku-shell-field-value ${mono ? "haku-shell-mono" : ""}`}>{children}</div>
    </div>
  );
}
