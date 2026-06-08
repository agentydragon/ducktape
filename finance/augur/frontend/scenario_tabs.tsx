// The active scenario's visual identity (position color + name), reused by every panel that scopes
// to the active scenario (selected-rollout overlay, events, terminal table) so they all
// announce *which* variant they're showing — not just the comparison header. Color matches the
// header and chart legend (`scenarioColor`, by position), so the badge reads as the same entity
// everywhere.
export function ScenarioBadge({ label, color, className = "" }) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`} data-product-active-scenario-badge="">
      <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: color }} aria-hidden="true" />
      <span className="font-medium">{label}</span>
    </span>
  );
}
